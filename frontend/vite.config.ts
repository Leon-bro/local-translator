import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "../backend/dist", emptyOutDir: true },
  server: {
    proxy: {
      // Сервер принимает изменяющие запросы только со своего Origin.
      "/api": { target: backend, changeOrigin: true, headers: { Origin: backend } },
    },
  },
});
