import { randomBytes } from "node:crypto";
import { execFileSync, spawn } from "node:child_process";
import { createServer } from "node:net";

function isPortAvailable(port) {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", (error) => {
      if (error.code === "EADDRINUSE") resolve(false);
      else reject(error);
    });
    server.once("listening", () => server.close(() => resolve(true)));
    server.listen(port, "127.0.0.1");
  });
}

async function clearPort(port) {
  let output = "";
  try {
    output = execFileSync("lsof", ["-tiTCP:" + port, "-sTCP:LISTEN"], {
      encoding: "utf8",
    });
  } catch (error) {
    if (error.status !== 1) throw error;
  }

  const pids = output.trim().split(/\s+/).filter(Boolean).map(Number);
  for (const pid of pids) {
    console.log(`Stopping process ${pid} listening on port ${port}...`);
    process.kill(pid, "SIGTERM");
  }

  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (await isPortAvailable(port)) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }

  throw new Error(`Port ${port} is still in use after stopping its listener.`);
}

try {
  await Promise.all([clearPort(1420), clearPort(8743)]);
} catch (error) {
  console.error(error.message);
  process.exit(1);
}

const token = randomBytes(32).toString("hex");
const children = new Set();
let stopping = false;

function start(command, args, env) {
  const child = spawn(command, args, {
    cwd: process.cwd(),
    env: { ...process.env, ...env },
    stdio: "inherit",
  });
  children.add(child);
  return child;
}

function stop(exitCode = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of children) {
    if (!child.killed) child.kill("SIGTERM");
  }
  process.exitCode = exitCode;
}

const backend = start(
  "uv",
  ["run", "python", "backend/main.py"],
  { JOB_APPLICANT_TOKEN: token },
);

const frontend = start(
  process.execPath,
  ["node_modules/vite/bin/vite.js", "--open"],
  {
    HUNTER_DEV_API_TOKEN: token,
    VITE_API_BASE_URL: "/api",
  },
);

backend.on("exit", (code, signal) => {
  if (!stopping) {
    console.error(`Backend stopped (${signal ?? `exit ${code ?? 1}`}).`);
    stop(code ?? 1);
  }
});

frontend.on("exit", (code, signal) => {
  if (!stopping) {
    console.error(`Frontend stopped (${signal ?? `exit ${code ?? 1}`}).`);
    stop(code ?? 1);
  }
});

backend.on("error", (error) => {
  console.error(`Could not start backend: ${error.message}`);
  stop(1);
});

frontend.on("error", (error) => {
  console.error(`Could not start frontend: ${error.message}`);
  stop(1);
});

process.on("SIGINT", () => stop());
process.on("SIGTERM", () => stop());
