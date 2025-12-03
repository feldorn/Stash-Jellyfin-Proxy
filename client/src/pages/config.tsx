import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { ArrowLeft, Save, AlertTriangle, Loader2 } from "lucide-react";
import { Link } from "wouter";
import { useToast } from "@/hooks/use-toast";

interface ProxyConfig {
  stashUrl: string;
  stashApiKey: string;
  proxyBind: string;
  proxyPort: number;
  sjsUser: string;
  sjsPassword: string;
  serverId: string;
  serverName: string;
  tagGroups: string;
  latestGroups: string;
  stashTimeout: number;
  stashRetries: number;
  logDir: string;
  logFile: string;
  logLevel: string;
  logMaxSizeMb: number;
  logBackupCount: number;
  uiPort: number;
}

export default function Config() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [hasChanges, setHasChanges] = useState(false);
  const [serverIdChanged, setServerIdChanged] = useState(false);

  const { data: config, isLoading } = useQuery<ProxyConfig>({
    queryKey: ["config"],
    queryFn: async () => {
      const res = await fetch("/api/config");
      if (!res.ok) throw new Error("Failed to fetch config");
      return res.json();
    },
  });

  const [formData, setFormData] = useState<Partial<ProxyConfig>>({});

  // Initialize form data when config loads
  const currentConfig = { ...config, ...formData };

  const mutation = useMutation({
    mutationFn: async (data: ProxyConfig) => {
      const res = await fetch("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      if (!res.ok) throw new Error("Failed to save config");
      return res.json();
    },
    onSuccess: () => {
      toast({
        title: "Configuration saved",
        description: "Restart the proxy for changes to take effect.",
      });
      setHasChanges(false);
      setServerIdChanged(false);
      queryClient.invalidateQueries({ queryKey: ["config"] });
    },
    onError: () => {
      toast({
        title: "Error",
        description: "Failed to save configuration.",
        variant: "destructive",
      });
    },
  });

  const handleChange = (field: keyof ProxyConfig, value: string | number) => {
    setFormData(prev => ({ ...prev, [field]: value }));
    setHasChanges(true);
    if (field === "serverId" && value !== config?.serverId) {
      setServerIdChanged(true);
    }
  };

  const handleSave = () => {
    if (config) {
      mutation.mutate({ ...config, ...formData } as ProxyConfig);
    }
  };

  if (isLoading) {
    return (
      <div className="p-6 md:p-8 flex items-center justify-center min-h-[400px]">
        <Loader2 className="w-8 h-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="p-6 md:p-8 space-y-6 max-w-4xl mx-auto animate-in slide-in-from-bottom-4 duration-500">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Link href="/">
            <Button variant="ghost" size="sm" className="font-mono" data-testid="button-back">
              <ArrowLeft className="w-4 h-4 mr-2" />
              BACK
            </Button>
          </Link>
          <div>
            <h1 className="text-2xl font-bold tracking-tight font-mono">CONFIGURATION</h1>
            <p className="text-muted-foreground font-mono text-sm mt-1">
              Manage proxy settings
            </p>
          </div>
        </div>
        <Button 
          className="font-mono" 
          onClick={handleSave}
          disabled={!hasChanges || mutation.isPending}
          data-testid="button-save"
        >
          {mutation.isPending ? (
            <Loader2 className="w-4 h-4 mr-2 animate-spin" />
          ) : (
            <Save className="w-4 h-4 mr-2" />
          )}
          SAVE CHANGES
        </Button>
      </div>

      {/* Server ID Warning */}
      {serverIdChanged && (
        <Alert variant="destructive" data-testid="alert-server-id-warning">
          <AlertTriangle className="h-4 w-4" />
          <AlertDescription>
            Changing the Server ID will require you to remove and re-add the server in Infuse.
          </AlertDescription>
        </Alert>
      )}

      {/* Stash Connection */}
      <Card className="bg-card border-border/50">
        <CardHeader>
          <CardTitle className="font-mono text-base">Stash Connection</CardTitle>
          <CardDescription className="font-mono text-xs">
            Connection settings for your Stash server
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-2">
            <Label htmlFor="stash-url" className="font-mono text-xs uppercase">Stash URL</Label>
            <Input 
              id="stash-url" 
              value={currentConfig.stashUrl || ""} 
              onChange={e => handleChange("stashUrl", e.target.value)}
              className="font-mono bg-background/50" 
              placeholder="https://stash.example.com"
              data-testid="input-stash-url"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="api-key" className="font-mono text-xs uppercase">API Key</Label>
            <Input 
              id="api-key" 
              type="password" 
              value={currentConfig.stashApiKey || ""} 
              onChange={e => handleChange("stashApiKey", e.target.value)}
              className="font-mono bg-background/50" 
              placeholder="Your Stash API key"
              data-testid="input-api-key"
            />
            <p className="text-[10px] text-muted-foreground">
              Get from Stash → Settings → Security → API Key
            </p>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label htmlFor="timeout" className="font-mono text-xs uppercase">Timeout (seconds)</Label>
              <Input 
                id="timeout" 
                type="number" 
                value={currentConfig.stashTimeout || 30} 
                onChange={e => handleChange("stashTimeout", parseInt(e.target.value) || 30)}
                className="font-mono bg-background/50" 
                data-testid="input-timeout"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="retries" className="font-mono text-xs uppercase">Retries</Label>
              <Input 
                id="retries" 
                type="number" 
                value={currentConfig.stashRetries || 3} 
                onChange={e => handleChange("stashRetries", parseInt(e.target.value) || 3)}
                className="font-mono bg-background/50" 
                data-testid="input-retries"
              />
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Proxy Settings */}
      <Card className="bg-card border-border/50">
        <CardHeader>
          <CardTitle className="font-mono text-base">Proxy Settings</CardTitle>
          <CardDescription className="font-mono text-xs">
            Network and authentication for the Jellyfin proxy
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label htmlFor="bind-addr" className="font-mono text-xs uppercase">Bind Address</Label>
              <Input 
                id="bind-addr" 
                value={currentConfig.proxyBind || "0.0.0.0"} 
                onChange={e => handleChange("proxyBind", e.target.value)}
                className="font-mono bg-background/50" 
                data-testid="input-bind-addr"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="port" className="font-mono text-xs uppercase">Port</Label>
              <Input 
                id="port" 
                type="number"
                value={currentConfig.proxyPort || 8096} 
                onChange={e => handleChange("proxyPort", parseInt(e.target.value) || 8096)}
                className="font-mono bg-background/50" 
                data-testid="input-port"
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label htmlFor="username" className="font-mono text-xs uppercase">Username</Label>
              <Input 
                id="username" 
                value={currentConfig.sjsUser || ""} 
                onChange={e => handleChange("sjsUser", e.target.value)}
                className="font-mono bg-background/50" 
                data-testid="input-username"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="password" className="font-mono text-xs uppercase">Password</Label>
              <Input 
                id="password" 
                type="password"
                value={currentConfig.sjsPassword || ""} 
                onChange={e => handleChange("sjsPassword", e.target.value)}
                className="font-mono bg-background/50" 
                data-testid="input-password"
              />
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Server Identity */}
      <Card className="bg-card border-border/50">
        <CardHeader>
          <CardTitle className="font-mono text-base">Server Identity</CardTitle>
          <CardDescription className="font-mono text-xs">
            How the proxy identifies itself to clients
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-2">
            <Label htmlFor="server-id" className="font-mono text-xs uppercase">Server ID</Label>
            <Input 
              id="server-id" 
              value={currentConfig.serverId || ""} 
              onChange={e => handleChange("serverId", e.target.value)}
              className="font-mono bg-background/50" 
              data-testid="input-server-id"
            />
            <p className="text-[10px] text-muted-foreground flex items-center gap-1">
              <AlertTriangle className="w-3 h-3 text-yellow-500" />
              Changing this will require re-pairing Infuse
            </p>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="server-name" className="font-mono text-xs uppercase">Server Name</Label>
            <Input 
              id="server-name" 
              value={currentConfig.serverName || "Stash Media Server"} 
              onChange={e => handleChange("serverName", e.target.value)}
              className="font-mono bg-background/50" 
              data-testid="input-server-name"
            />
          </div>
        </CardContent>
      </Card>

      {/* Library Organization */}
      <Card className="bg-card border-border/50">
        <CardHeader>
          <CardTitle className="font-mono text-base">Library Organization</CardTitle>
          <CardDescription className="font-mono text-xs">
            How content is organized in Infuse
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-2">
            <Label htmlFor="tag-groups" className="font-mono text-xs uppercase">Tag Groups</Label>
            <Input 
              id="tag-groups" 
              value={currentConfig.tagGroups || ""} 
              onChange={e => handleChange("tagGroups", e.target.value)}
              className="font-mono bg-background/50" 
              placeholder="Tag1, Tag2, Tag3"
              data-testid="input-tag-groups"
            />
            <p className="text-[10px] text-muted-foreground">
              Comma-separated tag names to show as top-level folders
            </p>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="latest-groups" className="font-mono text-xs uppercase">Latest Groups</Label>
            <Input 
              id="latest-groups" 
              value={currentConfig.latestGroups || "Scenes"} 
              onChange={e => handleChange("latestGroups", e.target.value)}
              className="font-mono bg-background/50" 
              placeholder="Scenes, Tag1"
              data-testid="input-latest-groups"
            />
            <p className="text-[10px] text-muted-foreground">
              Libraries to show on Infuse home screen. "Scenes" = all scenes.
            </p>
          </div>
        </CardContent>
      </Card>

      {/* Logging */}
      <Card className="bg-card border-border/50">
        <CardHeader>
          <CardTitle className="font-mono text-base">Logging</CardTitle>
          <CardDescription className="font-mono text-xs">
            Log file settings
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-2">
            <Label htmlFor="log-level" className="font-mono text-xs uppercase">Log Level</Label>
            <Select 
              value={currentConfig.logLevel || "INFO"} 
              onValueChange={v => handleChange("logLevel", v)}
            >
              <SelectTrigger className="font-mono bg-background/50" data-testid="select-log-level">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="DEBUG">DEBUG</SelectItem>
                <SelectItem value="INFO">INFO</SelectItem>
                <SelectItem value="WARNING">WARNING</SelectItem>
                <SelectItem value="ERROR">ERROR</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label htmlFor="log-dir" className="font-mono text-xs uppercase">Log Directory</Label>
              <Input 
                id="log-dir" 
                value={currentConfig.logDir || "."} 
                onChange={e => handleChange("logDir", e.target.value)}
                className="font-mono bg-background/50" 
                data-testid="input-log-dir"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="log-file" className="font-mono text-xs uppercase">Log File</Label>
              <Input 
                id="log-file" 
                value={currentConfig.logFile || "stash_jellyfin_proxy.log"} 
                onChange={e => handleChange("logFile", e.target.value)}
                className="font-mono bg-background/50" 
                data-testid="input-log-file"
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label htmlFor="log-max-size" className="font-mono text-xs uppercase">Max Size (MB)</Label>
              <Input 
                id="log-max-size" 
                type="number"
                value={currentConfig.logMaxSizeMb || 10} 
                onChange={e => handleChange("logMaxSizeMb", parseInt(e.target.value) || 10)}
                className="font-mono bg-background/50" 
                data-testid="input-log-max-size"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="log-backups" className="font-mono text-xs uppercase">Backup Count</Label>
              <Input 
                id="log-backups" 
                type="number"
                value={currentConfig.logBackupCount || 3} 
                onChange={e => handleChange("logBackupCount", parseInt(e.target.value) || 3)}
                className="font-mono bg-background/50" 
                data-testid="input-log-backups"
              />
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Web UI */}
      <Card className="bg-card border-border/50">
        <CardHeader>
          <CardTitle className="font-mono text-base">Web UI</CardTitle>
          <CardDescription className="font-mono text-xs">
            Settings for this configuration interface
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-2">
            <Label htmlFor="ui-port" className="font-mono text-xs uppercase">UI Port</Label>
            <Input 
              id="ui-port" 
              type="number"
              value={currentConfig.uiPort || 8097} 
              onChange={e => handleChange("uiPort", parseInt(e.target.value) || 8097)}
              className="font-mono bg-background/50" 
              data-testid="input-ui-port"
            />
            <p className="text-[10px] text-muted-foreground">
              Port for this web interface (separate from proxy port)
            </p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
