// Read one page of the current user's usage ledger through the loaded client API.
// Module IDs are version specific. No login state, request headers, or UI is changed.
(() => {
  const key = __PROBE_KEY__, chunkKey = __CHUNK_KEY__, moduleId = __API_MODULE__;
  if (Object.hasOwn(globalThis, key)) throw Error('probe already exists');
  const chunks = window[chunkKey];
  if (!Array.isArray(chunks)) throw Error('verified client runtime is unavailable');
  let require;
  const entry = [[key], {}, r => { require = r; }];
  chunks.push(entry);
  const at = chunks.indexOf(entry);
  if (at >= 0) chunks.splice(at, 1);
  if (!require?.m?.[moduleId]) throw Error('verified API module is unavailable');
  const api = require(moduleId);
  if (typeof api.M?.AGWGetUsageTimeline !== 'function') throw Error('usage API is unavailable');
  const state = {done: false, started_at_ms: Date.now()};
  Object.defineProperty(globalThis, key, {value: state, configurable: true});
  const cleanup = () => { if (globalThis[key] === state) delete globalThis[key]; };
  state.cleanup_timer = setTimeout(cleanup, 60000);
  // Query exactly once. The server still enforces the user's existing permissions.
  Promise.resolve().then(() => api.M.AGWGetUsageTimeline({page_size: 20})).then(
    response => { state.response = response; },
    error => { state.error_type = String(error?.name || 'Error').slice(0, 80); }
  ).finally(() => { state.finished_at_ms = Date.now(); state.done = true; });
  return {started: true, page_size: 20, automatic_cleanup_ms: 60000};
})()
