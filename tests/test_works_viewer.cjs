const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../douyin_user_monitor/web/works.html"), "utf8");
const inline = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\n\s*init\(\);\s*$/, "");
const videoItem = Object.freeze({
  aweme_id: "7001", media_type: "video", desc: "video", file_count: 3,
  cover_index: 0, play_index: 1, archive_index: 2, has_fast_play: true,
  duration_ms: 10000, total_size_bytes: 1000, share_url: "https://example.test/video",
});

function element() {
  return {
    innerHTML: "", textContent: "", href: "", hidden: false,
    classList: { add() {}, remove() {} },
    addEventListener() {},
    removeAttribute(name) { delete this[name]; },
  };
}

function pageContext() {
  const nodes = new Map();
  const handlers = {};
  const page = { video: null };
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  const context = vm.createContext({
    URLSearchParams,
    API_BASE: "/api/monitor",
    window: { location: { search: "?user_id=user-1" }, innerWidth: 1000 },
    document: {
      body: get("body"), getElementById: get,
      addEventListener(name, handler) { handlers[name] = handler; },
      querySelector(selector) { return selector === "#viewerStage video" ? page.video : null; },
      createElement: element,
    },
    formatTime: (value) => value || "unknown",
    api() { throw new Error("unexpected API request"); },
  });
  vm.runInContext(inline, context);
  return Object.assign(page, {
    context, get, handlers,
    run: (code) => vm.runInContext(code, context),
    show(item) {
      context.testItem = item;
      vm.runInContext("state.items = [testItem]; openViewer(0);", context);
    },
  });
}

test("gallery fetches the cover image without creating video elements", () => {
  const page = pageContext();
  page.context.testItem = videoItem;
  const card = page.run("renderCard(testItem, 0)");
  assert.match(card, /<img[^>]+media\/0/);
  assert.match(card, /loading="lazy" decoding="async"/);
  assert.doesNotMatch(card, /<video|media\/1|media\/2/);
});

test("viewer uses the selected playback index and poster, with original as separate download", () => {
  const page = pageContext();
  page.show(videoItem);
  const stage = page.get("viewerStage").innerHTML;
  assert.match(stage, /<video[^>]+src="[^"]+\/media\/1"/);
  assert.doesNotMatch(stage, /direct=1/);
  assert.match(stage, /poster="[^"]+\/media\/0"/);
  assert.doesNotMatch(stage, /media\/2/);
  assert.equal(page.get("viewerOriginalLink").href, "/api/monitor/users/user-1/archive/7001/media/2");
  assert.equal(page.get("viewerOriginalLink").hidden, false);
  assert.match(html, /id="viewerOriginalLink" download/);
});

test("card and video poster prefer the independent cover URL", () => {
  const page = pageContext();
  const coverUrl = `/api/monitor/users/user-1/archive/7001/cover?v=${"a".repeat(64)}`;
  page.context.testItem = { ...videoItem, cover_url: coverUrl };
  const card = page.run("renderCard(testItem, 0)");
  assert.ok(card.includes(`src="${coverUrl}"`));
  assert.doesNotMatch(card, /\/media\/0|<video/);
  page.show(page.context.testItem);
  const stage = page.get("viewerStage").innerHTML;
  assert.ok(stage.includes(`poster="${coverUrl}"`));
  assert.match(stage, /src="[^"]+\/media\/1"/);
  assert.doesNotMatch(stage, /direct=1/);
  assert.doesNotMatch(stage, /\/media\/0/);
});

test("independent covers work when legacy cover indices are absent", () => {
  const page = pageContext();
  const coverUrl = `/api/monitor/users/user-1/archive/7001/cover?v=${"b".repeat(64)}`;
  page.context.testItem = { ...videoItem, cover_index: null, cover_url: coverUrl };
  assert.ok(page.run("renderCard(testItem, 0)").includes(`src="${coverUrl}"`));
  page.show(page.context.testItem);
  assert.ok(page.get("viewerStage").innerHTML.includes(`poster="${coverUrl}"`));
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

test("video arrow keys leave the current player in place", () => {
  const page = pageContext();
  page.show(videoItem);
  let restarts = 0;
  page.video = { pause() { restarts += 1; }, removeAttribute() {}, load() {} };
  const stage = page.get("viewerStage").innerHTML;
  page.handlers.keydown({ key: "ArrowRight" });
  page.handlers.keydown({ key: "ArrowLeft" });
  assert.equal(restarts, 0);
  assert.equal(page.get("viewerStage").innerHTML, stage);
});

test("closing the viewer explicitly aborts the current media source", () => {
  const page = pageContext();
  page.show(videoItem);
  const events = [];
  page.video = {
    pause() { events.push("pause"); },
    removeAttribute(name) { events.push(`remove:${name}`); },
    load() { events.push("load"); },
  };
  page.context.closeViewer();
  assert.deepEqual(events, ["pause", "remove:src", "load"]);
  assert.equal(page.get("viewerStage").innerHTML, "");
  assert.equal(page.run("state.viewerIndex"), -1);
});

test("missing or failing playback never substitutes the original video", () => {
  const page = pageContext();
  page.show({ ...videoItem, play_index: null });
  assert.doesNotMatch(page.get("viewerStage").innerHTML, /<video/);
  assert.match(page.get("viewerStage").innerHTML, /没有可播放文件/);
  page.show(videoItem);
  const source = page.get("viewerStage").innerHTML;
  page.context.showVideoError();
  assert.equal(page.get("viewerStage").innerHTML, source);
  assert.match(page.get("viewerMeta").textContent, /加载失败/);
});

test("image navigation uses only content indices and excludes cover", () => {
  const page = pageContext();
  page.show({ ...videoItem, media_type: "image", image_indices: [2, 4], file_count: 5 });
  assert.match(page.get("viewerStage").innerHTML, /media\/2/);
  assert.equal(page.get("viewerOriginalLink").hidden, true);
  page.context.stepImage(1);
  assert.match(page.get("viewerStage").innerHTML, /media\/4/);
  assert.doesNotMatch(page.get("viewerStage").innerHTML, /media\/0/);
  page.show({ ...videoItem, media_type: "image", image_indices: [], file_count: 1 });
  assert.doesNotMatch(page.get("viewerStage").innerHTML, /<img/);
});

test("loading another page retains existing cover elements until the response arrives", async () => {
  const page = pageContext();
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
