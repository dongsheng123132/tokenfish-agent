/*
  TokenFish 微信付款页（pay.html）：读 URL 的 # 片段（浏览器不会把它发给服务器），
  用同目录的 qrcode.js（qrcode-generator，MIT）在本地画出微信付款二维码，
  不调用任何外部出图服务。

  参数（#wx=...&amt=...&order=...）：
    wx    微信付款码链接，必须以 weixin:// 开头
    amt   金额（整数元），1 到 500
    order 订单号（字母、数字、下划线、短横线）

  安全约定：所有来自 URL 的内容只通过 textContent / setAttribute 进入页面，
  从不拼 HTML 字符串，也不执行任何来自 URL 的代码。二维码画在 canvas 上，
  再转成 img 的 data URL（这样在微信里可以长按识别）。
*/
(function () {
  "use strict";

  var ORDER_RE = /^[A-Za-z0-9_\-]{1,100}$/;
  var AMOUNT_RE = /^\d{1,3}$/;

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

  function showError(text) {
    var box = byId("pay-message");
    box.textContent = text;
    box.removeAttribute("hidden");
  }

  // 每个模块取整数像素边长，模块之间不留缝也不重叠；静区 4 个模块（规范要求）。
  function drawQr(text) {
    var qr = qrcode(0, "M");
    qr.addData(text);
    qr.make();
    var n = qr.getModuleCount();
    var quiet = 4;
    var total = n + quiet * 2;
    var cell = Math.max(4, Math.floor(320 / total));
    var size = total * cell;
    var canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    var ctx = canvas.getContext("2d");
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, size, size);
    ctx.fillStyle = "#000000";
    for (var r = 0; r < n; r++) {
      for (var c = 0; c < n; c++) {
        if (qr.isDark(r, c)) {
          ctx.fillRect((c + quiet) * cell, (r + quiet) * cell, cell, cell);
        }
      }
    }
    return canvas.toDataURL("image/png");
  }

  function render() {
    var params = readParams();
    var wx = params.get("wx");
    var amt = params.get("amt");
    var order = params.get("order");

    if (wx === null && amt === null && order === null) {
      showError("这里是微信付款页。请回到 AI 对话说「充值 10 元」，选微信，点它给你的付款链接打开；直接访问这个页面是看不到内容的。");
      return;
    }
    if (
      typeof wx !== "string" ||
      wx.indexOf("weixin://") !== 0 ||
      wx.length > 1000 ||
      typeof amt !== "string" ||
      !AMOUNT_RE.test(amt) ||
      Number(amt) < 1 ||
      Number(amt) > 500 ||
      typeof order !== "string" ||
      !ORDER_RE.test(order)
    ) {
      showError("链接无效或不完整。请回到 AI 对话重新说「充值 N 元」，获取新的付款链接。");
      return;
    }

    var dataUrl;
    try {
      dataUrl = drawQr(wx);
    } catch (err) {
      showError("二维码生成失败。请回到 AI 对话重新获取付款链接，或改用支付宝。");
      return;
    }

    byId("pay-amount").textContent = "¥" + amt + " 元";
    byId("pay-order").textContent = order;
    var img = document.createElement("img");
    img.setAttribute("src", dataUrl);
    img.setAttribute("alt", "微信付款二维码");
    byId("pay-qr").appendChild(img);
    byId("pay-body").removeAttribute("hidden");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
