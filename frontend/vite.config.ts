// Vite config with merged Vitest `test` block (design §I.6 dev proxy, plan.md P3 step 6).
// Imported from "vitest/config" rather than "vite" so the `test` key type-checks
// without a second competing config file.
import { defineConfig } from "vitest/config"
import vue from "@vitejs/plugin-vue"

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/chat": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/books": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    // Node-26/jsdom localStorage shadowing fix -- see testSetup.ts.
    setupFiles: ["./src/testSetup.ts"],
  },
})
