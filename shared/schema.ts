import { z } from "zod";

// Proxy configuration schema matching stash_jellyfin_proxy.conf
export const proxyConfigSchema = z.object({
  // Connection settings
  stashUrl: z.string().url(),
  stashApiKey: z.string(),
  proxyBind: z.string().default("0.0.0.0"),
  proxyPort: z.number().int().min(1).max(65535).default(8096),
  
  // Auth settings
  sjsUser: z.string(),
  sjsPassword: z.string(),
  
  // Server identity
  serverId: z.string(),
  serverName: z.string().optional(),
  
  // Library config
  tagGroups: z.string().optional(),
  latestGroups: z.string().optional(),
  
  // Performance settings
  stashTimeout: z.number().int().min(1).default(30),
  stashRetries: z.number().int().min(0).default(3),
  
  // Logging settings
  logDir: z.string().optional(),
  logFile: z.string().optional(),
  logLevel: z.enum(["DEBUG", "INFO", "WARNING", "ERROR"]).default("INFO"),
  logMaxSizeMb: z.number().int().min(0).default(10),
  logBackupCount: z.number().int().min(0).default(3),
  
  // UI settings
  uiPort: z.number().int().min(1).max(65535).default(8097),
});

export type ProxyConfig = z.infer<typeof proxyConfigSchema>;

// Status response from proxy
export const proxyStatusSchema = z.object({
  running: z.boolean(),
  version: z.string().optional(),
  proxyBind: z.string(),
  proxyPort: z.number(),
  stashConnected: z.boolean(),
  stashVersion: z.string().optional(),
  stashUrl: z.string(),
});

export type ProxyStatus = z.infer<typeof proxyStatusSchema>;

// Active stream info
export const activeStreamSchema = z.object({
  sceneId: z.string(),
  title: z.string(),
  startedAt: z.string(),
});

export type ActiveStream = z.infer<typeof activeStreamSchema>;

// Log entry
export const logEntrySchema = z.object({
  timestamp: z.string(),
  level: z.string(),
  message: z.string(),
});

export type LogEntry = z.infer<typeof logEntrySchema>;
