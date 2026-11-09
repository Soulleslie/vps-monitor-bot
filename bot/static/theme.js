"use strict";
// 外观：浅色 / 深色 / 跟随系统。在 <head> 中同步加载，页面绘制前就应用，避免闪烁。
// 选择只保存在当前浏览器（localStorage）；读不到时默认跟随系统。
(function () {
  var KEY = "vrb-theme";
  function read() {
    try { var v = localStorage.getItem(KEY); return v === "light" || v === "dark" ? v : "system"; }
    catch (_) { return "system"; }
  }
  function apply(mode) {
    var root = document.documentElement;
    if (mode === "light" || mode === "dark") root.setAttribute("data-theme", mode);
    else root.removeAttribute("data-theme");
  }
  window.vrbTheme = {
    get: read,
    set: function (mode) {
      try {
        if (mode === "light" || mode === "dark") localStorage.setItem(KEY, mode);
        else localStorage.removeItem(KEY);
      } catch (_) { /* 无法保存时只对当前页面生效 */ }
      apply(mode);
    },
  };
  apply(read());
})();
