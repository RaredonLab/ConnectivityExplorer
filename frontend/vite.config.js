import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { readFileSync } from "fs";

const { version } = JSON.parse(readFileSync("./package.json", "utf8"));

export default defineConfig({
  define: {
    // Injected at build time from package.json — single source of truth.
    __APP_VERSION__: JSON.stringify(version),
  },
  plugins: [react()],
  server: {
    // 5173, not 3000: docker compose binds 3000 for the production frontend, so a
    // default of 3000 made `npm run dev` collide with a running container.
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
