import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter } from "react-router-dom";
import "./index.css";
import "./i18n";
import App from "./App";
import { initTheme } from "./lib/theme";

// Apply the persisted theme before first paint to avoid a flash of light mode.
initTheme();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <HashRouter>
      <App />
    </HashRouter>
  </StrictMode>
);
