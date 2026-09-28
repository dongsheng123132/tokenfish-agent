/*
  TokenFish 静态站共享脚本：复制安装消息到剪贴板。
  纯原生 JS，无依赖，无打包步骤。
*/
(function () {
  "use strict";

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.left = "-1000px";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (err) {
      ok = false;
    }
    document.body.removeChild(ta);
    return ok;
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(
        function () {
          return true;
        },
        function () {
          return fallbackCopy(text);
        }
      );
    }
    return Promise.resolve(fallbackCopy(text));
  }

  function markCopied(btn) {
    var original = btn.getAttribute("data-original-label") || btn.textContent;
    btn.setAttribute("data-original-label", original);
    btn.textContent = "已复制，去 AI 对话里粘贴";
    btn.classList.add("tf-btn-copied");
    window.clearTimeout(btn._tfResetTimer);
    btn._tfResetTimer = window.setTimeout(function () {
      btn.textContent = original;
      btn.classList.remove("tf-btn-copied");
    }, 2200);
  }

  function markFailed(btn) {
    var original = btn.getAttribute("data-original-label") || btn.textContent;
    btn.setAttribute("data-original-label", original);
    btn.textContent = "复制失败，请手动选择文本";
    window.clearTimeout(btn._tfResetTimer);
    btn._tfResetTimer = window.setTimeout(function () {
      btn.textContent = original;
    }, 2600);
  }

  function initCopyButtons() {
    var buttons = document.querySelectorAll("[data-copy-text]");
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var text = btn.getAttribute("data-copy-text") || "";
        if (!text) {
          return;
        }
        copyText(text).then(function (ok) {
          if (ok) {
            markCopied(btn);
          } else {
            markFailed(btn);
          }
        });
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCopyButtons);
  } else {
    initCopyButtons();
  }
})();
