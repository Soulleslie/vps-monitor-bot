"use strict";
// 外观：浅色 / 深色 / 跟随系统。在 <head> 中同步加载，页面绘制前就应用，避免闪烁。
// 选择只保存在当前浏览器（localStorage）；读不到时默认跟随系统。
// 同时更新手机浏览器顶部状态栏/地址栏颜色（theme-color）和原生控件配色（color-scheme）。
(function () {
  var KEY = "vrb-theme";
  // 与 style.css 中顶栏（--sidebar）颜色一致
  var BAR = { light: "#ffffff", dark: "#0e1117" };
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function read() {
    try { var v = localStorage.getItem(KEY); return v === "light" || v === "dark" ? v : "system"; }
    catch (_) { return "system"; }
  }
  function effective(mode) {
    if (mode === "light" || mode === "dark") return mode;
    return media && media.matches ? "dark" : "light";
  }
  function meta(name) {
    var el = document.querySelector('meta[name="' + name + '"]');
    if (!el) {
      el = document.createElement("meta");
      el.setAttribute("name", name);
      document.head.appendChild(el);
    }
    return el;
  }
  function apply(mode) {
    var root = document.documentElement, eff = effective(mode);
    if (mode === "light" || mode === "dark") root.setAttribute("data-theme", mode);
    else root.removeAttribute("data-theme");
    meta("theme-color").setAttribute("content", BAR[eff]);
    meta("color-scheme").setAttribute("content", mode === "system" ? "light dark" : eff);
  }

  window.vrbTheme = {
    get: read,
    effective: function () { return effective(read()); },
    set: function (mode) {
      try {
        if (mode === "light" || mode === "dark") localStorage.setItem(KEY, mode);
        else localStorage.removeItem(KEY);
      } catch (_) { /* 无法保存时只对当前页面生效 */ }
      apply(mode);
    },
  };
  // 跟随系统时，系统切换深浅色后同步更新状态栏颜色（页面颜色由 CSS 自动切换）
  if (media) {
    var onChange = function () { if (read() === "system") apply("system"); };
    if (media.addEventListener) media.addEventListener("change", onChange);
    else if (media.addListener) media.addListener(onChange);
  }
  apply(read());
})();
