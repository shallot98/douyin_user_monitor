const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const Core = require(path.join(__dirname, "../douyin_user_monitor/web/static/swipe-core.js"));

test("滑动判定：距离过阈值或快速轻扫才翻页，边界不越界", () => {
  const base = { viewportHeight: 800, canNext: true, canPrevious: true };
  assert.equal(Core.resolveSwipe({ ...base, deltaY: -40, elapsedMs: 400 }), "stay");
  assert.equal(Core.resolveSwipe({ ...base, deltaY: -150, elapsedMs: 400 }), "next");
  assert.equal(Core.resolveSwipe({ ...base, deltaY: 150, elapsedMs: 400 }), "previous");
  // 轻扫：30px / 40ms = 0.75px/ms
  assert.equal(Core.resolveSwipe({ ...base, deltaY: -30, elapsedMs: 40 }), "next");
  assert.equal(Core.resolveSwipe({ ...base, deltaY: -150, elapsedMs: 400, canNext: false }), "stay");
  assert.equal(Core.resolveSwipe({ ...base, deltaY: 150, elapsedMs: 400, canPrevious: false }), "stay");
});

test("readQuery：沿用作品库参数，去掉类型/视图/页码，默认随机", () => {
  const params = Core.readQuery("?media_type=image&view=wall&size=3&page=4&outfit=a,b&q=&score_min=3");
  assert.equal(params.get("media_type"), null);
  assert.equal(params.get("view"), null);
  assert.equal(params.get("page"), null);
  assert.equal(params.get("q"), null);
  assert.equal(params.get("outfit"), "a,b");
  assert.equal(params.get("sort"), "random");
  // 非随机排序丢掉种子
  assert.equal(Core.readQuery("?sort=newest&seed=5").get("seed"), null);
  assert.equal(Core.readQuery("?seed=5").get("seed"), "5");
});

test("pageQuery：默认排序与种子不写进 URL", () => {
  assert.equal(Core.pageQuery(Core.readQuery("?seed=9&score_min=2")), "score_min=2");
  assert.equal(Core.pageQuery(Core.readQuery("?sort=newest")), "sort=newest");
});

test("apiQuery：强制只要视频，近 N 天换算成日期，随机沿用服务端种子", () => {
  const now = Date.UTC(2026, 9, 1, 20, 0, 0);  // 上海时间 10-02 04:00
  const params = Core.readQuery("?range=7&outfit=a");
  const query = Core.apiQuery(params, { page: 2, pageSize: 20, seed: 42, now });
  assert.equal(query.get("media_type"), "video");
  assert.equal(query.get("range"), null);
  assert.equal(query.get("date_from"), "2026-09-26");
  assert.equal(query.get("seed"), "42");
  assert.equal(query.get("page"), "2");
  assert.equal(query.get("page_size"), "20");
  assert.equal(Core.apiQuery(Core.readQuery("?sort=score"), { seed: 42 }).get("seed"), null);
  // 自定义区间原样透传
  const custom = Core.apiQuery(Core.readQuery("?date_from=2026-01-01&date_to=2026-02-01"), {});
  assert.equal(custom.get("date_from"), "2026-01-01");
  assert.equal(custom.get("date_to"), "2026-02-01");
});

test("面板外的条件计数与清除", () => {
  const params = Core.readQuery("?exclude=outfit:a,category:ad&chest_min=2&range=30&outfit=b&q=x");
  assert.equal(Core.otherConditionCount(params), 4);
  const cleared = Core.clearOtherConditions(params);
  assert.equal(Core.otherConditionCount(cleared), 0);
  assert.equal(cleared.get("outfit"), "b");
  assert.equal(cleared.get("q"), "x");
  assert.equal(cleared.get("sort"), "random");
});

test("toggleListValue：加入/移除，空了删参数", () => {
  let params = new URLSearchParams("outfit=a");
  params = Core.toggleListValue(params, "outfit", "b");
  assert.equal(params.get("outfit"), "a,b");
  params = Core.toggleListValue(params, "outfit", "a");
  params = Core.toggleListValue(params, "outfit", "b");
  assert.equal(params.has("outfit"), false);
});

test("mergeItems：跨页去重、跳过没有播放文件的", () => {
  const items = [];
  const seen = new Set();
  const page1 = [
    { user_id: "u1", aweme_id: "1", play_index: 0 },
    { user_id: "u1", aweme_id: "2", play_index: null },
    { user_id: "u2", aweme_id: "1", play_index: 1 },
  ];
  assert.equal(Core.mergeItems(items, page1, seen), 2);
  assert.equal(Core.mergeItems(items, [{ user_id: "u1", aweme_id: "1", play_index: 0 }], seen), 0);
  assert.deepEqual(items.map(Core.itemKey), ["u1/1", "u2/1"]);
});

test("直链缓存：按用户分批、失败退代理、单条复用在途请求", async () => {
  let clock = 1_000_000;
  const calls = [];
  const cache = Core.createDirectCache(async (userId, ids) => {
    calls.push([userId, [...ids]]);
    if (userId === "bad") throw new Error("boom");
    return {
      ttl_seconds: 600,
      urls: Object.fromEntries(ids.map((id) => [id, id === "x" ? "javascript:alert(1)" : `https://cdn.test/${id}`])),
    };
  }, () => clock);
  const items = [
    { user_id: "u1", aweme_id: "a" }, { user_id: "u2", aweme_id: "b" },
    { user_id: "u1", aweme_id: "x" }, { user_id: "bad", aweme_id: "c" },
  ];
  await cache.prefetch(items);
  assert.deepEqual(calls, [["u1", ["a", "x"]], ["u2", ["b"]], ["bad", ["c"]]]);
  assert.equal(cache.peek("a"), "https://cdn.test/a");
  assert.equal(cache.peek("x"), null, "非 http(s) 直链一律走代理");
  assert.equal(cache.peek("c"), null, "接口失败的走代理");
  // 已缓存不再请求
  assert.equal(await cache.resolve(items[0]), "https://cdn.test/a");
  assert.equal(calls.length, 3);
  // 过期后重取
  clock += 600 * 1000;
  assert.equal(cache.peek("a"), undefined);
  assert.equal(await cache.resolve(items[0]), "https://cdn.test/a");
  assert.deepEqual(calls[3], ["u1", ["a"]]);
  cache.invalidate("a");
  assert.equal(cache.peek("a"), undefined);
});
