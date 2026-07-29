// Phase 10 (tests): Vitest `test.setupFiles` shim.
//
// Node 26 ships an experimental `globalThis.localStorage` accessor (an
// EXPERIMENTAL global backed by `--localstorage-file`; without that flag it
// resolves to `undefined`). Vitest 4's jsdom environment copies `window`'s
// own properties onto `globalThis` only when `globalThis` doesn't already
// own that key -- but Node's `localStorage` getter already occupies the
// slot before jsdom ever runs, so jsdom's real `Storage` instance on
// `window.localStorage` never reaches the bare `localStorage` global that
// `state/session.ts` (and any other module) reads. Without this shim every
// persistence test fails with "Cannot read properties of undefined
// (reading 'clear')". Re-point `globalThis.localStorage` at jsdom's real
// instance once, after the jsdom environment has set up `window`.
//
// Quirk: the real instance must be reached through the `jsdom` global (the
// JSDOM object the environment stashes on the global before setup files run),
// NOT through `window` -- Vitest's `populateGlobal` assigns
// `global.window = global`, so in here `window === globalThis` and
// `window.localStorage` is the very `undefined` Node accessor being replaced.
// `Storage` IS in Vitest's copied-keys list, so it already resolves to jsdom's
// class and `Storage.prototype` spies apply to this instance.
const { window: jsdomWindow } = (globalThis as unknown as { jsdom: { window: Window } }).jsdom

Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  enumerable: true,
  value: jsdomWindow.localStorage,
})
