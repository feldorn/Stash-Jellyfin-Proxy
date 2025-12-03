import { Link, useLocation } from "wouter";
import { Terminal, Settings, FileText, Activity } from "lucide-react";
import { cn } from "@/lib/utils";

export default function Layout({ children }: { children: React.ReactNode }) {
  const [location] = useLocation();

  const navItems = [
    { href: "/", icon: Activity, label: "Status" },
    { href: "/config", icon: Settings, label: "Configuration" },
    { href: "/logs", icon: FileText, label: "Logs" },
  ];

  return (
    <div className="min-h-screen flex flex-col md:flex-row">
      {/* Sidebar */}
      <aside className="w-full md:w-64 bg-card border-b md:border-r border-border flex flex-col">
        <div className="p-6 border-b border-border">
          <div className="flex items-center gap-2 text-primary">
            <Terminal className="w-6 h-6" />
            <h1 className="font-bold tracking-tight">STASH<span className="text-muted-foreground">PROXY</span></h1>
          </div>
          <div className="mt-1 text-xs text-muted-foreground font-mono">v3.63</div>
        </div>

        <nav className="flex-1 p-4 space-y-1">
          {navItems.map((item) => (
            <Link key={item.href} href={item.href}>
              <div
                className={cn(
                  "flex items-center gap-3 px-3 py-2 rounded-md text-sm font-medium transition-colors cursor-pointer font-mono",
                  location === item.href
                    ? "bg-primary/10 text-primary"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted/50"
                )}
              >
                <item.icon className="w-4 h-4" />
                {item.label}
              </div>
            </Link>
          ))}
        </nav>

      </aside>

      {/* Main Content */}
      <main className="flex-1 bg-background overflow-y-auto">
        {children}
      </main>
    </div>
  );
}
