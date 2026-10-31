const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const WEB = path.join(__dirname, "../douyin_user_monitor/web");
const html = fs.readFileSync(path.join(WEB, "works.html"), "utf8");
const viewerJs = fs.readFileSync(path.join(WEB, "static/works-viewer.js"), "utf8");
// 去掉末尾的 init()（它会立刻加载第 1 页），测试里只挂播放器
const inline = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\n\s*init\(\);\s*$/, "\n    initViewer();\n");
const videoItem = Object.freeze({
  aweme_id: "7001", media_type: "video", desc: "video", file_count: 3,
  cover_index: 0, play_index: 1, archive_index: 2, has_fast_play: true,
  duration_ms: 10000, total_size_bytes: 1000, share_url: "https://example.test/video",
});

// 极简 DOM：只实现播放器与作品页用到的那部分
function element(tagName = "div") {
  const node = {
    tagName: tagName.toUpperCase(), id: "", className: "", textContent: "", title: "", href: "",
    hidden: false, disabled: false, src: "", poster: "", style: {}, dataset: {}, attributes: {},
    children: [], parentElement: null, listeners: {}, events: [], isConnected: true,
    classList: {
      set: new Set(),
      add(name) { this.set.add(name); }, remove(name) { this.set.delete(name); },
      contains(name) { return this.set.has(name); },
    },
    get innerHTML() { return this._html || ""; },
    set innerHTML(value) {
      this.children.forEach((child) => { child.isConnected = false; });
      this.children = [];
      this._html = String(value);
    },
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); },
    dispatch(name, event = {}) { (this.listeners[name] || []).forEach((handler) => handler(event)); },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    removeAttribute(name) { this.events.push(`remove:${name}`); delete this.attributes[name]; delete this[name]; },
    appendChild(child) { child.parentElement = node; child.isConnected = true; node.children.push(child); return child; },
    insertBefore(child, ref) {
      if (!ref) return node.appendChild(child);
      child.parentElement = node;
      node.children.splice(node.children.indexOf(ref), 0, child);
      return child;
    },
    querySelector(selector) {
      return node.children.find((child) => child.tagName === selector.toUpperCase()) || null;
    },
    remove() {},
    closest() { return null; },
    pause() { node.events.push("pause"); },
    load() { node.events.push("load"); },
    play() { node.events.push("play"); return Promise.resolve(); },
    scrollIntoView() {},
    getBoundingClientRect() { return { left: 0, top: 0 }; },
    offsetWidth: 360, offsetHeight: 700,
  };
  return node;
}

function defaultResponse(apiPath) {
  if (apiPath.includes("/playback-urls")) return { urls: {}, image_urls: {}, ttl_seconds: 600 };
  if (apiPath.includes("/prefetch")) return {};
  return { items: [], page: 1, total: 0, total_pages: 1 };
}

function pageContext(search = "?user_id=user-1") {
  const nodes = new Map();
  const handlers = {};
  const calls = [];
  const lazyIds = new Set(["viewer", "works-viewer-style"]);
  const register = (node) => { if (node.id) nodes.set(node.id, node); return node; };
  const get = (id) => {
    if (!nodes.has(id)) {
      if (lazyIds.has(id)) return null;
      nodes.set(id, Object.assign(element(), { id }));
    }
    return nodes.get(id);
  };
  const body = Object.assign(element("body"), { appendChild: (node) => register(node) });
  const head = Object.assign(element("head"), { appendChild: (node) => register(node) });
  const page = { respond: defaultResponse, calls };
  // 只模拟同步 popstate：back() 退一条并立刻派发给页面
  const historyStack = [null];
  const history = {
    get state() { return historyStack[historyStack.length - 1]; },
    pushState(state) { historyStack.push(state); },
    back() {
      if (historyStack.length > 1) historyStack.pop();
      handlers["window:popstate"]?.({ state: history.state, stopImmediatePropagation() {} });
    },
  };
  const context = vm.createContext({
    history,
    URLSearchParams, console, setTimeout, clearTimeout, requestAnimationFrame: (fn) => fn(),
    API_BASE: "/api/monitor",
    location: { search, origin: "https://monitor.test" },
    innerWidth: 1000, innerHeight: 900,
    parent: { postMessage(message) { calls.push({ postMessage: message }); } },
    addEventListener(name, handler) { handlers[`window:${name}`] = handler; },
    Image: function Image() {},
    document: {
      body, head, getElementById: get,
      addEventListener(name, handler) { handlers[name] = handler; },
      querySelector() { return null; },
      querySelectorAll() { return []; },
      createElement: element,
    },
    formatTime: (value) => value || "unknown",
    api(apiPath, options) {
      calls.push({ path: apiPath, options });
      return Promise.resolve(page.respond(apiPath, options));
    },
  });
  context.window = context;
  vm.runInContext(viewerJs, context);
  vm.runInContext(inline, context);
  const stage = () => get("viewerStage");
  return Object.assign(page, {
    context, get, handlers, stage, history, historyStack,
    video: () => stage().querySelector("video"),
    image: () => stage().querySelector("img"),
    run: (code) => vm.runInContext(code, context),
    show(item) {
      context.testItem = item;
      vm.runInContext("state.items = [testItem]; WorksViewer.open(0);", context);
    },
  });
}

const flush = async () => { for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

test("gallery fetches the cover image without creating video elements", () => {
  const page = pageContext();
  page.context.testItem = videoItem;
  const card = page.run("renderCard(testItem, 0)");
  assert.match(card, /<img[^>]+media\/0/);
  assert.match(card, /loading="lazy" decoding="async"/);
  assert.doesNotMatch(card, /<video|media\/1|media\/2/);
});

test("viewer DOM is injected by the shared module, not duplicated in the page", () => {
  assert.doesNotMatch(html, /id="viewer"/);
  assert.match(html, /<script src="\/static\/works-viewer\.js\?v=back-20261031"><\/script>/);
  const page = pageContext();
  assert.ok(page.get("viewer"));
  assert.match(page.get("viewer").innerHTML, /id="viewerOriginalLink" download/);
  assert.ok(page.get("works-viewer-style"));
});

test("viewer falls back to the proxy playback index with poster; original is a separate download", async () => {
  const page = pageContext();
  page.show(videoItem);
  await flush();
  const video = page.video();
  assert.equal(video.src, "/api/monitor/users/user-1/archive/7001/media/1");
  assert.equal(video.dataset.mode, "proxy");
  assert.equal(video.poster, "/api/monitor/users/user-1/archive/7001/media/0");
  assert.equal(page.get("viewerOriginalLink").href, "/api/monitor/users/user-1/archive/7001/media/2");
  assert.equal(page.get("viewerOriginalLink").hidden, false);
  assert.match(page.get("viewerMeta").textContent, /代理$/);
});

test("direct CDN url is used first and disabled after two failures", async () => {
  const page = pageContext();
  page.respond = (apiPath) => (apiPath.includes("/playback-urls")
    ? { urls: { 7001: "https://cdn.test/a.mp4", 7002: "https://cdn.test/b.mp4" }, ttl_seconds: 600 }
    : {});
  page.context.list = [videoItem, { ...videoItem, aweme_id: "7002" }];
  page.run("state.items = list; WorksViewer.prefetchDirectUrls(state.items);");
  await flush();
  page.run("WorksViewer.open(0)");
  let video = page.video();
  assert.equal(video.src, "https://cdn.test/a.mp4");
  video.dispatch("error");
  assert.equal(video.src, "/api/monitor/users/user-1/archive/7001/media/1");
  assert.match(page.get("viewerMeta").textContent, /代理（直连失败）/);
  page.run("WorksViewer.open(1)");
  video = page.video();
  assert.equal(video.src, "https://cdn.test/b.mp4");
  video.dispatch("error");
  assert.match(page.get("viewerMeta").textContent, /已停用/);
  // 停用后再次打开直接走代理，并在出画面后预取相邻作品
  page.run("WorksViewer.open(1)");
  video = page.video();
  assert.equal(video.dataset.mode, "proxy");
  video.dispatch("loadeddata");
  assert.ok(page.calls.some((call) => call.path === "/users/user-1/archive/7002/prefetch"));
});

test("direct-url batches and media urls follow each item's user_id", async () => {
  const page = pageContext();
  page.context.list = [
    { ...videoItem, aweme_id: "a1", user_id: "u-a" },
    { ...videoItem, aweme_id: "b1", user_id: "u-b" },
    { ...videoItem, aweme_id: "a2", user_id: "u-a" },
    { ...videoItem, aweme_id: "d1" },
  ];
  page.run("WorksViewer.prefetchDirectUrls(list)");
  const batches = page.calls.filter((call) => call.path.includes("/playback-urls"))
    .map((call) => [call.path, JSON.parse(call.options.body).aweme_ids]);
  assert.deepEqual(batches, [
    ["/users/u-a/archive/playback-urls", ["a1", "a2"]],
    ["/users/u-b/archive/playback-urls", ["b1"]],
    ["/users/user-1/archive/playback-urls", ["d1"]],
  ]);
  assert.equal(page.run("WorksViewer.archiveMediaUrl(list[1], 3)"), "/api/monitor/users/u-b/archive/b1/media/3");
  await flush();
  page.show(page.context.list[1]);
  assert.equal(page.video().src, "/api/monitor/users/u-b/archive/b1/media/1");
});

test("card and video poster prefer the independent cover URL", async () => {
  const page = pageContext();
  const coverUrl = `/api/monitor/users/user-1/archive/7001/cover?v=${"a".repeat(64)}`;
  page.context.testItem = { ...videoItem, cover_url: coverUrl };
  const card = page.run("renderCard(testItem, 0)");
  assert.ok(card.includes(`src="${coverUrl}"`));
  assert.doesNotMatch(card, /\/media\/0|<video/);
  page.show(page.context.testItem);
  await flush();
  assert.equal(page.video().poster, coverUrl);
  assert.match(page.video().src, /\/media\/1$/);
});

test("independent covers work when legacy cover indices are absent", () => {
  const page = pageContext();
  const coverUrl = `/api/monitor/users/user-1/archive/7001/cover?v=${"b".repeat(64)}`;
  page.context.testItem = { ...videoItem, cover_index: null, cover_url: coverUrl };
  assert.ok(page.run("renderCard(testItem, 0)").includes(`src="${coverUrl}"`));
  page.show(page.context.testItem);
  assert.equal(page.video().poster, coverUrl);
});

test("cover URLs only accept the current work's local versioned endpoint", () => {
  const page = pageContext();
  const urls = ["javascript:alert(1)", "https://elsewhere.test/cover.jpg",
    `/api/monitor/users/another-user/archive/7001/cover?v=${"a".repeat(64)}`];
  for (const coverUrl of urls) {
    page.context.testItem = { ...videoItem, cover_index: null, cover_url: coverUrl };
    assert.doesNotMatch(page.run("renderCard(testItem, 0)"), /<img/);
  }
});

test("video arrow keys leave the current player in place", async () => {
  const page = pageContext();
  page.show(videoItem);
  await flush();
  const video = page.video();
  page.handlers.keydown({ key: "ArrowRight" });
  page.handlers.keydown({ key: "ArrowLeft" });
  assert.equal(page.video(), video);
  assert.doesNotMatch(video.events.join(","), /pause/);
});

test("closing the viewer explicitly aborts the current media source", async () => {
  const page = pageContext();
  page.show(videoItem);
  await flush();
  const video = page.video();
  video.events.length = 0;
  page.handlers.keydown({ key: "Escape" });
  assert.deepEqual(video.events, ["pause", "remove:src", "load"]);
  assert.equal(page.stage().innerHTML, "");
  assert.equal(page.run("WorksViewer.isOpen()"), false);
  assert.equal(page.run("state.playingIndex"), -1);
});

test("system back closes the viewer; stepping works does not stack history", () => {
  const page = pageContext();
  page.context.testItems = [videoItem, { ...videoItem, aweme_id: "7002" }];
  page.run("state.items = testItems; WorksViewer.open(0);");
  assert.equal(page.historyStack.length, 2);
  page.run("WorksViewer.stepWork(1)");
  assert.equal(page.run("WorksViewer.currentAwemeId()"), "7002");
  assert.equal(page.historyStack.length, 2);
  page.history.back(); // iOS 右滑 / 浏览器返回
  assert.equal(page.run("WorksViewer.isOpen()"), false);
  assert.equal(page.historyStack.length, 1);
  assert.equal(page.stage().innerHTML, "");
});

test("close button goes through history and ignores a second press before popstate", () => {
  const page = pageContext();
  page.show(videoItem);
  let backs = 0;
  page.history.back = () => { backs += 1; };
  page.run("WorksViewer.close(); WorksViewer.close();");
  assert.equal(backs, 1);
  assert.equal(page.run("WorksViewer.isOpen()"), true);
  page.historyStack.pop();
  page.handlers["window:popstate"]({ state: null, stopImmediatePropagation() {} });
  assert.equal(page.run("WorksViewer.isOpen()"), false);
});

test("Esc with the viewer closed asks the embedding overlay to close", () => {
  const page = pageContext("?user_id=user-1&embed=1");
  page.handlers.keydown({ key: "Escape" });
  const messages = page.calls.filter((call) => call.postMessage).map((call) => JSON.stringify(call.postMessage));
  assert.deepEqual(messages, ['{"type":"works:close"}']);
});

test("missing or failing playback never substitutes the original video", () => {
  const page = pageContext();
  page.show({ ...videoItem, play_index: null });
  assert.equal(page.video(), null);
  assert.match(page.stage().innerHTML, /没有可播放文件/);
  page.show(videoItem);
  const video = page.video();
  page.run("WorksViewer.showVideoError()");
  assert.equal(page.video(), video);
  assert.match(page.get("viewerMeta").textContent, /加载失败/);
});

test("image navigation uses only content indices and excludes cover", async () => {
  const page = pageContext();
  page.show({ ...videoItem, media_type: "image", image_indices: [2, 4], file_count: 5 });
  await flush();
  assert.match(page.image().src, /media\/2$/);
  assert.equal(page.get("viewerOriginalLink").hidden, true);
  page.run("WorksViewer.stepImage(1)");
  assert.match(page.image().src, /media\/4$/);
  assert.match(page.stage().innerHTML, /2 \/ 2/);
  page.show({ ...videoItem, media_type: "image", image_indices: [], file_count: 1 });
  assert.equal(page.image(), null);
});

test("per-image direct urls are used and fall back to the proxy on error", async () => {
  const page = pageContext();
  page.respond = () => ({ image_urls: { 7001: ["https://cdn.test/1.jpg", "javascript:alert(1)"] }, ttl_seconds: 600 });
  page.show({ ...videoItem, media_type: "image", image_indices: [2, 4] });
  await flush();
  const image = page.image();
  assert.equal(image.src, "https://cdn.test/1.jpg");
  image.dispatch("error");
  assert.equal(image.src, "/api/monitor/users/user-1/archive/7001/media/2");
  page.run("WorksViewer.stepImage(1)");
  await flush();
  assert.match(page.image().src, /\/media\/4$/);
});

test("content label panel never shows the letters AI", () => {
  const page = pageContext();
  page.show({ ...videoItem, content_filter: { status: "error", error: "timeout" } });
  assert.match(page.get("viewerContentLabels").innerHTML, /未检测：timeout/);
  page.show({ ...videoItem, content_filter: {
    status: "ok", verdict: "reject", category: "ad", ad_type: "selling", confidence: 0.9, summary: "s",
    tags: { content: ["舞蹈"] }, appearance: { borderline: { score: 3, chest: 1, hip: 1, figure: 1, overall: 1, camera: 0, motion: 0 } },
  } });
  const panel = page.get("viewerContentLabels").innerHTML;
  assert.match(panel, /广告·带货 90%/);
  assert.match(panel, /擦边 3\/5/);
  assert.doesNotMatch(panel, /AI/);
});

test("stepping past the page end loads the next page and plays its first work", async () => {
  const page = pageContext();
  page.context.list = [videoItem];
  page.run("state.items = list; state.page = 1; state.pageSize = 1; state.total = 2; state.totalPages = 2; WorksViewer.open(0);");
  assert.equal(page.get("viewerCounter").textContent, "1 / 2");
  assert.equal(page.get("viewerNextWork").disabled, false);
  page.respond = (apiPath) => (apiPath.includes("/archive?")
    ? { items: [{ ...videoItem, aweme_id: "7002" }], page: 2, page_size: 1, total: 2, total_pages: 2 }
    : defaultResponse(apiPath));
  page.handlers.keydown({ key: "ArrowDown", preventDefault() {} });
  await flush();
  assert.equal(page.run("WorksViewer.currentAwemeId()"), "7002");
  assert.equal(page.get("viewerCounter").textContent, "2 / 2");
  assert.equal(page.get("viewerNextWork").disabled, true);
  assert.equal(page.run("state.playingIndex"), 0);
});

test("loading another page retains existing cover elements until the response arrives", async () => {
  const page = pageContext();
  await flush();
  page.context.testItem = videoItem;
  page.run("state.items = [testItem]; state.total = 2; state.totalPages = 2; renderAll();");
  const grid = page.get("worksGrid");
  let replacements = 0;
  let content = grid.innerHTML;
  Object.defineProperty(grid, "innerHTML", {
    get() { return content; },
    set(value) { content = value; replacements += 1; },
  });
  let respond;
  page.context.api = () => new Promise((resolve) => { respond = resolve; });
  const loading = page.context.loadPage(2);
  assert.equal(replacements, 0);
  respond({ items: [videoItem], page: 2, total: 2, total_pages: 2 });
  await loading;
  assert.equal(replacements, 1);
});

test("a failed cover request displays an error without requesting video", () => {
  const page = pageContext();
  let replacement;
  page.context.showCoverError({ replaceWith(value) { replacement = value; } });
  assert.equal(replacement.textContent, "封面加载失败");
  assert.equal(replacement.className, "work-cover-empty");
});

test("syncAfterReload keeps the same user's work when aweme_id repeats across users", () => {
  const page = pageContext();
  page.context.list = [
    { ...videoItem, aweme_id: "same", user_id: "u-a" },
    { ...videoItem, aweme_id: "same", user_id: "u-b" },
  ];
  page.run("state.items = list; WorksViewer.open(1);");
  // 列表重新加载后，正在播的仍是 u-b 那一条（只比 aweme_id 会错认成 u-a）
  page.run("state.items = list.map((item) => ({ ...item })); WorksViewer.syncAfterReload();");
  assert.equal(page.run("state.playingIndex"), 1);
});

test("switching works inside the viewer does not scroll the list; closing reveals the last card once", async () => {
  const page = pageContext();
  page.context.list = [videoItem, { ...videoItem, aweme_id: "7002" }];
  page.run(`
    scrollLog = [];
    markPlayingCard = (scroll) => scrollLog.push(["mark", state.playingIndex, Boolean(scroll)]);
    revealCard = (index) => scrollLog.push(["reveal", index]);
    state.items = list; WorksViewer.open(0);
  `);
  page.handlers.keydown({ key: "ArrowDown", preventDefault() {} });
  await flush();
  assert.equal(page.run("WorksViewer.currentAwemeId()"), "7002");
  page.handlers.keydown({ key: "Escape" });
  assert.deepEqual(JSON.parse(page.run("JSON.stringify(scrollLog)")),
    [["mark", 0, false], ["mark", 1, false], ["mark", -1, false], ["reveal", 1]]);
});
