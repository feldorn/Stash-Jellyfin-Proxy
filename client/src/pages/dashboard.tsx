import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { 
  Activity, 
  Server, 
  Wifi,
  WifiOff,
  Play,
  RefreshCw,
  Settings,
  ExternalLink
} from "lucide-react";
import { Link } from "wouter";

interface ProxyStatus {
  running: boolean;
  version?: string;
  proxyBind: string;
  proxyPort: number;
  stashConnected: boolean;
  stashVersion?: string;
  stashUrl: string;
}

interface LogEntry {
  timestamp: string;
  level: string;
  message: string;
}

interface ActiveStream {
  sceneId: string;
  title: string;
  startedAt: string;
}

export default function Dashboard() {
  const { data: status, isLoading: statusLoading, refetch: refetchStatus } = useQuery<ProxyStatus>({
    queryKey: ["status"],
    queryFn: async () => {
      const res = await fetch("/api/status");
      if (!res.ok) throw new Error("Failed to fetch status");
      return res.json();
    },
    refetchInterval: 5000,
  });

  const { data: logsData, isLoading: logsLoading, refetch: refetchLogs } = useQuery<{ entries: LogEntry[] }>({
    queryKey: ["logs"],
    queryFn: async () => {
      const res = await fetch("/api/logs");
      if (!res.ok) throw new Error("Failed to fetch logs");
      return res.json();
    },
    refetchInterval: 3000,
  });

  const { data: streamsData } = useQuery<{ streams: ActiveStream[] }>({
    queryKey: ["streams"],
    queryFn: async () => {
      const res = await fetch("/api/streams");
      if (!res.ok) throw new Error("Failed to fetch streams");
      return res.json();
    },
    refetchInterval: 5000,
  });

  const handleReload = () => {
    refetchStatus();
    refetchLogs();
  };

  const logs = logsData?.entries || [];
  const streams = streamsData?.streams || [];

  return (
    <div className="p-6 md:p-8 space-y-6 max-w-7xl mx-auto animate-in fade-in duration-500">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight font-mono">STASH-JELLYFIN PROXY</h1>
          <p className="text-muted-foreground font-mono text-sm mt-1">
            {status?.version || "v3.63"}
          </p>
        </div>
        <div className="flex gap-2">
          <Button 
            variant="outline" 
            size="sm" 
            className="font-mono"
            onClick={handleReload}
            data-testid="button-reload"
          >
            <RefreshCw className="w-4 h-4 mr-2" />
            RELOAD
          </Button>
          <Link href="/config">
            <Button size="sm" className="font-mono" data-testid="button-settings">
              <Settings className="w-4 h-4 mr-2" />
              SETTINGS
            </Button>
          </Link>
        </div>
      </div>

      {/* Status Grid */}
      <div className="grid gap-4 md:grid-cols-3">
        {/* Proxy Status */}
        <Card className="bg-card border-border/50" data-testid="card-proxy-status">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium font-mono text-muted-foreground">
              PROXY STATUS
            </CardTitle>
            <Server className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            {statusLoading ? (
              <div className="text-muted-foreground font-mono">Loading...</div>
            ) : (
              <>
                <div className="flex items-center gap-2">
                  <span className={`w-2 h-2 rounded-full ${status?.running ? 'bg-green-500' : 'bg-red-500'}`} />
                  <span className="text-xl font-bold font-mono" data-testid="text-proxy-status">
                    {status?.running ? "Running" : "Stopped"}
                  </span>
                </div>
                <p className="text-xs text-muted-foreground mt-1 font-mono" data-testid="text-proxy-address">
                  {status?.proxyBind}:{status?.proxyPort}
                </p>
              </>
            )}
          </CardContent>
        </Card>

        {/* Stash Connection */}
        <Card className="bg-card border-border/50" data-testid="card-stash-status">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium font-mono text-muted-foreground">
              STASH SERVER
            </CardTitle>
            {status?.stashConnected ? (
              <Wifi className="h-4 w-4 text-green-500" />
            ) : (
              <WifiOff className="h-4 w-4 text-red-500" />
            )}
          </CardHeader>
          <CardContent>
            {statusLoading ? (
              <div className="text-muted-foreground font-mono">Loading...</div>
            ) : (
              <>
                <div className="flex items-center gap-2">
                  <span className={`w-2 h-2 rounded-full ${status?.stashConnected ? 'bg-green-500' : 'bg-red-500'}`} />
                  <span className="text-xl font-bold font-mono" data-testid="text-stash-status">
                    {status?.stashConnected ? "Connected" : "Disconnected"}
                  </span>
                </div>
                <p className="text-xs text-muted-foreground mt-1 font-mono" data-testid="text-stash-version">
                  {status?.stashVersion || status?.stashUrl || "Not connected"}
                </p>
              </>
            )}
          </CardContent>
        </Card>

        {/* Active Streams */}
        <Card className="bg-card border-border/50" data-testid="card-active-streams">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium font-mono text-muted-foreground">
              ACTIVE STREAMS
            </CardTitle>
            <Activity className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold font-mono" data-testid="text-stream-count">
              {streams.length}
            </div>
            <p className="text-xs text-muted-foreground mt-1 font-mono">
              {streams.length === 0 ? "No active streams" : "Currently watching"}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Active Streams List */}
      {streams.length > 0 && (
        <Card className="bg-card border-border/50">
          <CardHeader>
            <CardTitle className="font-mono text-sm text-muted-foreground uppercase">
              Active Streams
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {streams.map((stream, i) => (
                <div 
                  key={stream.sceneId} 
                  className="flex items-center gap-3 p-2 bg-background/50 rounded"
                  data-testid={`stream-item-${i}`}
                >
                  <Play className="w-4 h-4 text-green-500" />
                  <div>
                    <span className="font-mono text-sm">{stream.title}</span>
                    <span className="text-muted-foreground font-mono text-xs ml-2">
                      ({stream.sceneId})
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Live Logs */}
      <Card className="bg-card border-border/50">
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="font-mono text-sm text-muted-foreground uppercase">
            Live Logs
          </CardTitle>
          <Link href="/logs">
            <Button variant="ghost" size="sm" className="font-mono text-xs" data-testid="link-view-logs">
              View Full <ExternalLink className="w-3 h-3 ml-1" />
            </Button>
          </Link>
        </CardHeader>
        <CardContent>
          <div className="bg-black/40 rounded-md p-4 h-[300px] overflow-y-auto font-mono text-xs space-y-1 border border-border/30" data-testid="log-viewer">
            {logsLoading ? (
              <div className="text-muted-foreground">Loading logs...</div>
            ) : logs.length === 0 ? (
              <div className="text-muted-foreground">No logs available. Start the proxy to see logs.</div>
            ) : (
              logs.slice(0, 50).map((entry, i) => (
                <div key={i} className="text-muted-foreground" data-testid={`log-entry-${i}`}>
                  <span className={`${
                    entry.level === 'ERROR' ? 'text-red-400' :
                    entry.level === 'WARNING' ? 'text-yellow-400' :
                    entry.level === 'INFO' ? 'text-blue-400' :
                    'text-muted-foreground'
                  }`}>
                    [{entry.timestamp.split(' ')[1]?.split(',')[0] || entry.timestamp}]
                  </span>
                  {' '}
                  <Badge variant="outline" className={`text-[10px] px-1 py-0 ${
                    entry.level === 'ERROR' ? 'border-red-500 text-red-400' :
                    entry.level === 'WARNING' ? 'border-yellow-500 text-yellow-400' :
                    entry.level === 'INFO' ? 'border-blue-500 text-blue-400' :
                    'border-gray-500 text-gray-400'
                  }`}>
                    {entry.level}
                  </Badge>
                  {' '}
                  <span className="text-foreground">{entry.message}</span>
                </div>
              ))
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
