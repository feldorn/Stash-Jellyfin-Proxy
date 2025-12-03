import type { Express } from "express";
import { createServer, type Server } from "http";
import * as fs from "fs";
import * as path from "path";
import { proxyConfigSchema } from "../shared/schema";

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

function parseBool(val: string | undefined, defaultVal: boolean): boolean {
  if (!val) return defaultVal;
  return val.toLowerCase() === "true" || val === "1";
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
    defaultPageSize: parseInt(raw.DEFAULT_PAGE_SIZE || "50", 10),
    maxPageSize: parseInt(raw.MAX_PAGE_SIZE || "200", 10),
    enableFilters: parseBool(raw.ENABLE_FILTERS, true),
    enableImageResize: parseBool(raw.ENABLE_IMAGE_RESIZE, true),
    imageCacheMaxSize: parseInt(raw.IMAGE_CACHE_MAX_SIZE || "100", 10),
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
    "# ---- Pagination Settings ----",
    "",
    `DEFAULT_PAGE_SIZE = ${api.defaultPageSize || 50}`,
    `MAX_PAGE_SIZE = ${api.maxPageSize || 200}`,
    "",
    "# ---- Feature Toggles ----",
    "",
    `ENABLE_FILTERS = ${api.enableFilters !== false}`,
    `ENABLE_IMAGE_RESIZE = ${api.enableImageResize !== false}`,
    `IMAGE_CACHE_MAX_SIZE = ${api.imageCacheMaxSize || 100}`,
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

function getLogPath(configPath: string | null): string {
  let logPath = "./stash_jellyfin_proxy.log";
  
  if (configPath) {
    try {
      const content = fs.readFileSync(configPath, "utf-8");
      const raw = parseConfigFile(content);
      const logDir = raw.LOG_DIR || ".";
      const logFile = raw.LOG_FILE || "stash_jellyfin_proxy.log";
      logPath = path.join(logDir, logFile);
    } catch (e) {
      // Use default
    }
  }
  
  return logPath;
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
      
      // Validate with zod schema (partial to allow missing optional fields)
      const result = proxyConfigSchema.safeParse(req.body);
      if (!result.success) {
        return res.status(400).json({ 
          error: "Invalid configuration", 
          details: result.error.errors 
        });
      }
      
      const newConfig = apiToConfigFormat(result.data);
      fs.writeFileSync(configPath, newConfig, "utf-8");
      
      res.json({ success: true });
    } catch (err) {
      res.status(500).json({ error: "Failed to write config" });
    }
  });
  
  // GET /api/status - Get proxy status
  app.get("/api/status", async (req, res) => {
    try {
      const configPath = findConfigPath();
      let config: any = {};
      
      if (configPath) {
        const content = fs.readFileSync(configPath, "utf-8");
        const raw = parseConfigFile(content);
        config = configToApiFormat(raw);
      }
      
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
      
      // If proxy is running, assume Stash is connected
      if (proxyRunning) {
        stashConnected = true;
      }
      
      res.json({
        running: proxyRunning,
        version: "v3.64",
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
      const logPath = getLogPath(configPath);
      const limit = parseInt(req.query.limit as string || "100", 10);
      
      if (!fs.existsSync(logPath)) {
        return res.json({ entries: [], logPath });
      }
      
      const content = fs.readFileSync(logPath, "utf-8");
      const lines = content.split("\n").filter(l => l.trim());
      
      // Parse last N lines
      const entries = lines
        .slice(-limit)
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
      const logPath = getLogPath(configPath);
      
      if (!fs.existsSync(logPath)) {
        return res.status(404).json({ error: "Log file not found" });
      }
      
      const filename = path.basename(logPath);
      res.setHeader("Content-Type", "text/plain");
      res.setHeader("Content-Disposition", `attachment; filename="${filename}"`);
      
      const stream = fs.createReadStream(logPath);
      stream.pipe(res);
    } catch (err) {
      res.status(500).json({ error: "Failed to download logs" });
    }
  });
  
  // GET /api/streams - Get active streams
  // Note: Would need IPC with Python proxy for real data
  app.get("/api/streams", (req, res) => {
    res.json({ streams: [] });
  });

  return httpServer;
}
