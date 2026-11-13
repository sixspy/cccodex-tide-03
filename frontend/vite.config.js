import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3233,
    strictPort: true,
    proxy: { "/api": "http://127.0.0.1:8223" },
  },
});
