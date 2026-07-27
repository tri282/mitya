import { createApp } from "vue"
import App from "./App.vue"

// Self-hosted Spectral (Open Gap #3): explicit per-subset, per-weight entrypoints
// only -- never the package barrel, which would pull latin-ext/vietnamese too.
// Weights per DESIGN.md: reading 400 (+400 italic), labels 500, send button 600.
// Cyrillic is loaded for the reading weights only (Russian appears in quoted
// corpus text, not in UI chrome).
import "@fontsource/spectral/latin-400.css"
import "@fontsource/spectral/latin-400-italic.css"
import "@fontsource/spectral/latin-500.css"
import "@fontsource/spectral/latin-600.css"
import "@fontsource/spectral/cyrillic-400.css"
import "@fontsource/spectral/cyrillic-400-italic.css"

import "./styles/base.css"

createApp(App).mount("#app")
