/// <reference types="vitest/config" />
import { defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

const browserDevToken = process.env.HUNTER_DEV_API_TOKEN;
const browserProxy: ProxyOptions | undefined = browserDevToken
  ? {
      target: "http://127.0.0.1:8743",
      changeOrigin: true,
      rewrite: (requestPath) => requestPath.replace(/^\/api/, ""),
      configure: (proxy) => {
        proxy.on("proxyReq", (proxyRequest) => {
          proxyRequest.setHeader("Authorization", `Bearer ${browserDevToken}`);
        });
      },
    }
  : undefined;

// https://vite.dev/config/
export default defineConfig(async () => ({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    proxy: browserProxy ? { "/api": browserProxy } : undefined,
    watch: {
      ignored: ["**/backend/**"],
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "text-summary", "html", "lcov"],
      reportsDirectory: "./coverage",
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.{test,spec}.{ts,tsx}",
        "src/test/**",
        "src/main.tsx",
        "src/vite-env.d.ts",
        "src/**/*.d.ts",
        "src/locales/**",
        "src/i18n/**",
      ],
    },
  },
}));
