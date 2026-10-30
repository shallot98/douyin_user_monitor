// 刷视频页的纯逻辑（无 DOM）：滑动判定、查询参数、直链缓存。浏览器挂 window.SwipeCore，node 测试 require。
// 滑动阈值参考 /root/aiyin 的 feed-navigation：距离过 18% 屏高或快速轻扫都算翻页。
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.SwipeCore = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const SWIPE_DISTANCE_RATIO = 0.18;
  const SWIPE_MIN_DISTANCE = 48;
  const FLICK_MIN_DISTANCE = 24;
  const FLICK_VELOCITY = 0.45;

  // 返回 "next" / "previous" / "stay"；deltaY < 0 为上滑（看下一个）
  function resolveSwipe({ deltaY, elapsedMs, viewportHeight, canNext, canPrevious }) {
    const distance = Math.abs(deltaY);
    const threshold = Math.max(SWIPE_MIN_DISTANCE, Math.floor(viewportHeight * SWIPE_DISTANCE_RATIO));
    const velocity = distance / Math.max(elapsedMs, 1);
    const far = distance >= threshold;
    const flick = distance >= FLICK_MIN_DISTANCE && velocity >= FLICK_VELOCITY;
    if (!far && !flick) return "stay";
    if (deltaY < 0) return canNext ? "next" : "stay";
    return canPrevious ? "previous" : "stay";
  }

  // ---- 查询参数：与作品库 URL 同一套参数名，作品库的筛选链接可以直接带过来 ----

  // 本页只刷视频；视图/卡片尺寸是作品库专用
  const DROPPED_PARAMS = ["media_type", "view", "size", "page", "page_size"];
  const DATE_PRESETS = ["7", "30", "90"];
  // 本页筛选面板直接编辑的参数；其余（日期、单项分、排除、分类……）原样保留
  const TAG_GROUPS = ["outfit", "content", "scene", "style", "subject", "features", "enhancers"];
  const EDITABLE_PARAMS = ["sort", "seed", "score_min", "q", "user_ids", ...TAG_GROUPS];
  const DEFAULT_SORT = "random";

  function parseList(value) {
    return [...new Set(String(value || "").split(",").map((part) => part.trim()).filter(Boolean))];
  }

  // 页面 URL → 规范化后的筛选参数（不含页码）。没写排序时默认随机
  function readQuery(search) {
    const params = new URLSearchParams(search);
    DROPPED_PARAMS.forEach((key) => params.delete(key));
    for (const key of [...params.keys()]) {
      if (!String(params.get(key) || "").trim()) params.delete(key);
    }
    if (!params.get("sort")) params.set("sort", DEFAULT_SORT);
    if (params.get("sort") !== "random") params.delete("seed");
    return params;
  }

  // 写回页面 URL 的形式：默认排序不写，随机种子不写（刷新即换一批）
  function pageQuery(params) {
    const next = new URLSearchParams(params);
    next.delete("seed");
    if (next.get("sort") === DEFAULT_SORT) next.delete("sort");
    return next.toString();
  }

  // 上海时区的“N 天前”日期，与作品库一致：近 N 天在请求时换算，不冻结在收藏那天
  function shanghaiDateDaysAgo(days, now = Date.now()) {
    return new Date(now + 8 * 3600 * 1000 - days * 86400 * 1000).toISOString().slice(0, 10);
  }

  function apiQuery(params, { page, pageSize, seed, now } = {}) {
    const next = new URLSearchParams(params);
    const range = next.get("range");
    next.delete("range");
    if (DATE_PRESETS.includes(range)) next.set("date_from", shanghaiDateDaysAgo(Number(range) - 1, now));
    next.set("media_type", "video");
    if (next.get("sort") === "random" && seed !== null && seed !== undefined && !next.get("seed")) {
      next.set("seed", String(seed));
    }
    next.set("page", String(page || 1));
    next.set("page_size", String(pageSize || 20));
    return next;
  }

  // 面板不直接编辑的条件个数（提示“另有 N 项作品库条件”）
  function otherConditionCount(params) {
    let count = 0;
    for (const key of new Set(params.keys())) {
      if (EDITABLE_PARAMS.includes(key)) continue;
      count += ["exclude", "category"].includes(key) ? parseList(params.get(key)).length : 1;
    }
    return count;
  }

  function clearOtherConditions(params) {
    const next = new URLSearchParams();
    EDITABLE_PARAMS.forEach((key) => {
      if (params.has(key)) next.set(key, params.get(key));
    });
    return next;
  }

  function toggleListValue(params, key, value) {
    const next = new URLSearchParams(params);
    const list = parseList(next.get(key));
    const index = list.indexOf(value);
    if (index >= 0) list.splice(index, 1);
    else list.push(value);
    if (list.length) next.set(key, list.join(","));
    else next.delete(key);
    return next;
  }

  // 作品列表合并：跨页去重（随机排序同一种子下不会重复，这里只是兜底），跳过没有可播放文件的
  function itemKey(item) {
    return `${item.user_id}/${item.aweme_id}`;
  }

  function playable(item) {
    return Boolean(item && item.aweme_id && item.user_id && item.play_index !== null && item.play_index !== undefined);
  }

  function mergeItems(existing, incoming, seen) {
    const added = [];
    for (const item of Array.isArray(incoming) ? incoming : []) {
      if (!playable(item)) continue;
      const key = itemKey(item);
      if (seen.has(key)) continue;
      seen.add(key);
      added.push(item);
    }
    existing.push(...added);
    return added.length;
  }

  // ---- 直链缓存：接口按用户解析，批量请求按 user_id 分组 ----
  // fetchBatch(userId, awemeIds) → { urls, errors, ttl_seconds }；url 为 null 表示只能走服务端代理
  const REFRESH_MARGIN_MS = 60 * 1000;
  const FAILURE_BACKOFF_MS = 60 * 1000;
  const BATCH_LIMIT = 100;

  function safeMediaUrl(value) {
    const text = String(value || "").trim();
    return /^https?:\/\//i.test(text) ? text : "";
  }

  function createDirectCache(fetchBatch, now = () => Date.now()) {
    const entries = new Map();
    const pending = new Map();

    function fresh(awemeId) {
      const entry = entries.get(awemeId);
      if (!entry || now() >= entry.expiresAt - REFRESH_MARGIN_MS) return undefined;
      return entry.url;
    }

    function request(userId, awemeIds) {
      const promise = Promise.resolve()
        .then(() => fetchBatch(userId, awemeIds))
        .then((data) => {
          const expiresAt = now() + Number(data?.ttl_seconds || 0) * 1000;
          const urls = data?.urls || {};
          // 解析失败/没回的作品这段时间内直接走代理
          awemeIds.forEach((awemeId) => {
            entries.set(awemeId, { url: safeMediaUrl(urls[awemeId]) || null, expiresAt });
          });
          const errors = data?.errors || {};
          if (Object.keys(errors).length) console.warn("部分作品直链解析失败，改走代理", errors);
        })
        .catch((error) => {
          console.warn("获取直链失败，改走代理", error);
          const expiresAt = now() + REFRESH_MARGIN_MS + FAILURE_BACKOFF_MS;
          awemeIds.forEach((awemeId) => entries.set(awemeId, { url: null, expiresAt }));
        })
        .finally(() => {
          awemeIds.forEach((awemeId) => {
            if (pending.get(awemeId) === promise) pending.delete(awemeId);
          });
        });
      promise.batchSize = awemeIds.length;
      awemeIds.forEach((awemeId) => pending.set(awemeId, promise));
      return promise;
    }

    function prefetch(items) {
      const groups = new Map();
      for (const item of items) {
        const awemeId = String(item.aweme_id);
        if (fresh(awemeId) !== undefined || pending.has(awemeId)) continue;
        if (!groups.has(item.user_id)) groups.set(item.user_id, []);
        groups.get(item.user_id).push(awemeId);
      }
      const requests = [];
      groups.forEach((ids, userId) => {
        for (let start = 0; start < ids.length; start += BATCH_LIMIT) {
          requests.push(request(userId, ids.slice(start, start + BATCH_LIMIT)));
        }
      });
      return Promise.all(requests);
    }

    // 字符串=直链；null=走代理
    async function resolve(item) {
      const awemeId = String(item.aweme_id);
      if (fresh(awemeId) === undefined) {
        // 整批要等最慢的那条；当前这条单独取更快，单条请求已在路上就复用
        const inflight = pending.get(awemeId);
        await (inflight && inflight.batchSize === 1 ? inflight : request(item.user_id, [awemeId]));
      }
      return fresh(awemeId) ?? null;
    }

    return {
      prefetch,
      resolve,
      peek: fresh,
      invalidate(awemeId) { entries.delete(String(awemeId)); },
    };
  }

  return {
    resolveSwipe,
    readQuery,
    pageQuery,
    apiQuery,
    otherConditionCount,
    clearOtherConditions,
    toggleListValue,
    parseList,
    mergeItems,
    itemKey,
    playable,
    createDirectCache,
    shanghaiDateDaysAgo,
    TAG_GROUPS,
    DEFAULT_SORT,
  };
});
