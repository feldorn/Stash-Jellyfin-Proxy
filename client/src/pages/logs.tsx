import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ArrowLeft, Download, RefreshCw, Search } from "lucide-react";
import { Link } from "wouter";

interface LogEntry {
  timestamp: string;
  level: string;
  message: string;
}

export default function Logs() {
  const [levelFilter, setLevelFilter] = useState<string>("ALL");
  const [searchQuery, setSearchQuery] = useState("");

  const { data: logsData, isLoading, refetch } = useQuery<{ entries: LogEntry[], logPath: string }>({
    queryKey: ["logs"],
    queryFn: async () => {
      const res = await fetch("/api/logs");
      if (!res.ok) throw new Error("Failed to fetch logs");
      return res.json();
    },
    refetchInterval: 3000,
  });

  const logs = logsData?.entries || [];
  
  const filteredLogs = logs.filter(entry => {
    const matchesLevel = levelFilter === "ALL" || entry.level === levelFilter;
    const matchesSearch = searchQuery === "" || 
      entry.message.toLowerCase().includes(searchQuery.toLowerCase()) ||
      entry.timestamp.includes(searchQuery);
    return matchesLevel && matchesSearch;
  });

  const handleDownload = () => {
    window.open("/api/logs/download", "_blank");
  };

  return (
    <div className="p-6 md:p-8 space-y-6 max-w-7xl mx-auto animate-in fade-in duration-500">
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
            <h1 className="text-2xl font-bold tracking-tight font-mono">LOGS</h1>
            <p className="text-muted-foreground font-mono text-sm mt-1">
              {logsData?.logPath || "Proxy log viewer"}
            </p>
          </div>
        </div>
        <div className="flex gap-2">
          <Button 
            variant="outline" 
            size="sm" 
            className="font-mono"
            onClick={() => refetch()}
            data-testid="button-refresh"
          >
            <RefreshCw className="w-4 h-4 mr-2" />
            REFRESH
          </Button>
          <Button 
            variant="outline" 
            size="sm" 
            className="font-mono"
            onClick={handleDownload}
            data-testid="button-download"
          >
            <Download className="w-4 h-4 mr-2" />
            DOWNLOAD
          </Button>
        </div>
      </div>

      {/* Filters */}
      <Card className="bg-card border-border/50">
        <CardContent className="pt-4">
          <div className="flex gap-4 items-center">
            <div className="flex items-center gap-2">
              <span className="text-xs font-mono text-muted-foreground">LEVEL:</span>
              <Select value={levelFilter} onValueChange={setLevelFilter}>
                <SelectTrigger className="w-[120px] font-mono bg-background/50" data-testid="select-level-filter">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="ALL">All</SelectItem>
                  <SelectItem value="DEBUG">DEBUG</SelectItem>
                  <SelectItem value="INFO">INFO</SelectItem>
                  <SelectItem value="WARNING">WARNING</SelectItem>
                  <SelectItem value="ERROR">ERROR</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex-1 relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
              <Input 
                placeholder="Search logs..." 
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
                className="font-mono pl-10 bg-background/50"
                data-testid="input-search"
              />
            </div>
            <div className="text-xs font-mono text-muted-foreground">
              {filteredLogs.length} / {logs.length} entries
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Log Viewer */}
      <Card className="bg-card border-border/50">
        <CardHeader>
          <CardTitle className="font-mono text-sm text-muted-foreground uppercase">
            Log Entries
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div 
            className="bg-black/40 rounded-md p-4 min-h-[500px] max-h-[70vh] overflow-y-auto font-mono text-xs space-y-1 border border-border/30"
            data-testid="log-viewer"
          >
            {isLoading ? (
              <div className="text-muted-foreground">Loading logs...</div>
            ) : filteredLogs.length === 0 ? (
              <div className="text-muted-foreground">
                {logs.length === 0 
                  ? "No logs available. Start the proxy to see logs."
                  : "No logs match the current filters."
                }
              </div>
            ) : (
              filteredLogs.map((entry, i) => (
                <div 
                  key={i} 
                  className="text-muted-foreground hover:bg-white/5 px-1 rounded"
                  data-testid={`log-entry-${i}`}
                >
                  <span className={`${
                    entry.level === 'ERROR' ? 'text-red-400' :
                    entry.level === 'WARNING' ? 'text-yellow-400' :
                    entry.level === 'INFO' ? 'text-blue-400' :
                    'text-muted-foreground'
                  }`}>
                    [{entry.timestamp}]
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
