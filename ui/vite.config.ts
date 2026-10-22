import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The production bundle is written into the Python package so `traceglass serve` can ship it.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../python/src/traceglass/_ui",
    emptyOutDir: true,
    chunkSizeWarningLimit: 4000,
    // One JS + one CSS file: lets `traceglass export` inline the whole UI into a single HTML file.
    cssCodeSplit: false,
    modulePreload: false,
  },
  server: {
    port: 5173,
    host: "127.0.0.1",
    proxy: { "/api": { target: "http://127.0.0.1:7357", changeOrigin: true } },
  },
});
