import type { Express } from "express";
import { createServer, type Server } from "http";
import * as fs from "fs";
import * as path from "path";

// Config file path - look for it in common locations
const CONFIG_PATHS = [
  "./stash_jellyfin_proxy.conf",
  "../stash_jellyfin_proxy.conf",
  path.join(process.cwd(), "stash_jellyfin_proxy.conf"),
];

function findConfigPath(): string | null {
  for (const p of CONFIG_PATHS) {
    if (fs.existsSync(p)) {
      return p;
    }
  }
  return null;
}

function parseConfigFile(content: string): Record<string, string> {
  const config: Record<string, string> = {};
  const lines = content.split("\n");
  
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith("#") || !trimmed.includes("=")) {
      continue;
    }
    
    const eqIndex = trimmed.indexOf("=");
    const key = trimmed.substring(0, eqIndex).trim();
    let value = trimmed.substring(eqIndex + 1).trim();
    
    // Remove quotes from string values
    if ((value.startsWith('"') && value.endsWith('"')) || 
        (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    
    config[key] = value;
  }
  
  return config;
}

function configToApiFormat(raw: Record<string, string>) {
  return {
    stashUrl: raw.STASH_URL || "http://localhost:9999",
    stashApiKey: raw.STASH_API_KEY || "",
    proxyBind: raw.PROXY_BIND || "0.0.0.0",
    proxyPort: parseInt(raw.PROXY_PORT || "8096", 10),
    sjsUser: raw.SJS_USER || "admin",
    sjsPassword: raw.SJS_PASSWORD || "",
    serverId: raw.SERVER_ID || "",
    serverName: raw.SERVER_NAME || "Stash Media Server",
    tagGroups: raw.TAG_GROUPS || "",
    latestGroups: raw.LATEST_GROUPS || "Scenes",
    stashTimeout: parseInt(raw.STASH_TIMEOUT || "30", 10),
    stashRetries: parseInt(raw.STASH_RETRIES || "3", 10),
    logDir: raw.LOG_DIR || ".",
    logFile: raw.LOG_FILE || "stash_jellyfin_proxy.log",
    logLevel: raw.LOG_LEVEL || "INFO",
    logMaxSizeMb: parseInt(raw.LOG_MAX_SIZE_MB || "10", 10),
    logBackupCount: parseInt(raw.LOG_BACKUP_COUNT || "3", 10),
    uiPort: parseInt(raw.UI_PORT || "8097", 10),
  };
}

function apiToConfigFormat(api: any): string {
  const lines = [
    "# Stash-Jellyfin Proxy Configuration",
    "# ===================================",
    "",
    "# ---- Connection Settings ----",
    "",
    `STASH_URL = "${api.stashUrl}"`,
    `PROXY_BIND = "${api.proxyBind}"`,
    `PROXY_PORT = ${api.proxyPort}`,
    `STASH_API_KEY = "${api.stashApiKey}"`,
    "",
    "# ---- User Credentials ----",
    "",
    `SJS_USER = "${api.sjsUser}"`,
    `SJS_PASSWORD = "${api.sjsPassword}"`,
    "",
    "# ---- Library Configuration ----",
    "",
    `TAG_GROUPS = "${api.tagGroups || ""}"`,
    `LATEST_GROUPS = "${api.latestGroups || "Scenes"}"`,
    "",
    "# ---- Server Identity ----",
    "",
    `SERVER_ID = "${api.serverId}"`,
    `SERVER_NAME = "${api.serverName || "Stash Media Server"}"`,
    "",
    "# ---- Performance Settings ----",
    "",
    `STASH_TIMEOUT = ${api.stashTimeout || 30}`,
    `STASH_RETRIES = ${api.stashRetries || 3}`,
    "",
    "# ---- Logging Settings ----",
    "",
    `LOG_DIR = "${api.logDir || "."}"`,
    `LOG_FILE = "${api.logFile || "stash_jellyfin_proxy.log"}"`,
    `LOG_LEVEL = "${api.logLevel || "INFO"}"`,
    `LOG_MAX_SIZE_MB = ${api.logMaxSizeMb || 10}`,
    `LOG_BACKUP_COUNT = ${api.logBackupCount || 3}`,
    "",
    "# ---- Web UI Settings ----",
    "",
    `UI_PORT = ${api.uiPort || 8097}`,
    "",
  ];
  
  return lines.join("\n");
}

function parseLogLine(line: string) {
  // Format: 2025-12-03 11:33:42,741 - stash-jellyfin-proxy - INFO - message
  const match = line.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - [^-]+ - (\w+) - (.*)$/);
  if (match) {
    return {
      timestamp: match[1],
      level: match[2],
      message: match[3],
    };
  }
  return null;
}

export async function registerRoutes(
  httpServer: Server,
  app: Express
): Promise<Server> {
  
  // GET /api/config - Read current configuration
  app.get("/api/config", (req, res) => {
    try {
      const configPath = findConfigPath();
      if (!configPath) {
        return res.status(404).json({ error: "Config file not found" });
      }
      
      const content = fs.readFileSync(configPath, "utf-8");
      const raw = parseConfigFile(content);
      const config = configToApiFormat(raw);
      
      res.json(config);
    } catch (err) {
      res.status(500).json({ error: "Failed to read config" });
    }
  });
  
  // PUT /api/config - Update configuration
  app.put("/api/config", (req, res) => {
    try {
      const configPath = findConfigPath();
      if (!configPath) {
        return res.status(404).json({ error: "Config file not found" });
      }
      
      const newConfig = apiToConfigFormat(req.body);
      fs.writeFileSync(configPath, newConfig, "utf-8");
      
      res.json({ success: true });
    } catch (err) {
      res.status(500).json({ error: "Failed to write config" });
    }
  });
  
  // GET /api/status - Get proxy status (mock for now, would connect to real proxy)
  app.get("/api/status", async (req, res) => {
    try {
      const configPath = findConfigPath();
      let config: any = {};
      
      if (configPath) {
        const content = fs.readFileSync(configPath, "utf-8");
        const raw = parseConfigFile(content);
        config = configToApiFormat(raw);
      }
      
      // Check if we can reach the proxy
      let proxyRunning = false;
      let stashConnected = false;
      let stashVersion = "";
      
      // Try to fetch from proxy's system info endpoint
      try {
        const proxyUrl = `http://localhost:${config.proxyPort || 8096}/System/Info/Public`;
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 2000);
        
        const response = await fetch(proxyUrl, { signal: controller.signal });
        clearTimeout(timeout);
        
        if (response.ok) {
          proxyRunning = true;
          const data = await response.json();
          stashVersion = data.Version || "";
        }
      } catch (e) {
        // Proxy not reachable
      }
      
      // If proxy is running, assume Stash is connected (proxy wouldn't start otherwise)
      if (proxyRunning) {
        stashConnected = true;
      }
      
      res.json({
        running: proxyRunning,
        version: "v3.63",
        proxyBind: config.proxyBind || "0.0.0.0",
        proxyPort: config.proxyPort || 8096,
        stashConnected,
        stashVersion,
        stashUrl: config.stashUrl || "",
      });
    } catch (err) {
      res.status(500).json({ error: "Failed to get status" });
    }
  });
  
  // GET /api/logs - Get recent log entries
  app.get("/api/logs", (req, res) => {
    try {
      const configPath = findConfigPath();
      let logPath = "./stash_jellyfin_proxy.log";
      
      if (configPath) {
        const content = fs.readFileSync(configPath, "utf-8");
        const raw = parseConfigFile(content);
        const logDir = raw.LOG_DIR || ".";
        const logFile = raw.LOG_FILE || "stash_jellyfin_proxy.log";
        logPath = path.join(logDir, logFile);
      }
      
      if (!fs.existsSync(logPath)) {
        return res.json({ entries: [], logPath });
      }
      
      const content = fs.readFileSync(logPath, "utf-8");
      const lines = content.split("\n").filter(l => l.trim());
      
      // Parse last 100 lines
      const entries = lines
        .slice(-100)
        .map(parseLogLine)
        .filter(Boolean)
        .reverse();
      
      res.json({ entries, logPath });
    } catch (err) {
      res.status(500).json({ error: "Failed to read logs" });
    }
  });
  
  // GET /api/logs/download - Download full log file
  app.get("/api/logs/download", (req, res) => {
    try {
      const configPath = findConfigPath();
      let logPath = "./stash_jellyfin_proxy.log";
      
      if (configPath) {
        const content = fs.readFileSync(configPath, "utf-8");
        const raw = parseConfigFile(content);
        const logDir = raw.LOG_DIR || ".";
        const logFile = raw.LOG_FILE || "stash_jellyfin_proxy.log";
        logPath = path.join(logDir, logFile);
      }
      
      if (!fs.existsSync(logPath)) {
        return res.status(404).json({ error: "Log file not found" });
      }
      
      res.download(logPath);
    } catch (err) {
      res.status(500).json({ error: "Failed to download logs" });
    }
  });
  
  // GET /api/streams - Get active streams (would need IPC with proxy)
  app.get("/api/streams", (req, res) => {
    // This would need inter-process communication with the Python proxy
    // For now, return empty array - can be enhanced later
    res.json({ streams: [] });
  });

  return httpServer;
}
