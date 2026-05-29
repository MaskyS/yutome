"use client";

import { useState } from "react";
import { Check, ChevronDown, Copy, Link2 } from "lucide-react";
import { Link } from "react-router";

import { Button } from "~/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "~/components/ui/dropdown-menu";

// Keeps the personal MCP endpoint reachable from every dashboard page now that
// Connect is no longer a tab — the home page still has the full connect hero.
export function McpMenu({ mcpUrl }: { mcpUrl: string }) {
  const [status, setStatus] = useState<"idle" | "copied" | "blocked">("idle");

  async function copy() {
    try {
      await navigator.clipboard.writeText(mcpUrl);
      setStatus("copied");
      setTimeout(() => setStatus("idle"), 1500);
    } catch {
      // Clipboard blocked (e.g. insecure context). Surface it instead of failing
      // silently; the URL above is select-all so the user can copy it manually.
      setStatus("blocked");
      setTimeout(() => setStatus("idle"), 5000);
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1.5">
          <Link2 className="size-3.5" />
          MCP
          <ChevronDown className="size-3.5 opacity-60" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        <DropdownMenuLabel>Your MCP endpoint</DropdownMenuLabel>
        <div className="bg-muted mx-1.5 mb-1 rounded-md px-2 py-1.5 font-mono text-xs break-all select-all">
          {mcpUrl}
        </div>
        {status === "blocked" ? (
          <p className="text-muted-foreground mx-1.5 mb-1 text-xs">
            Copy was blocked — select the URL above and press ⌘C / Ctrl+C.
          </p>
        ) : null}
        <DropdownMenuItem
          onSelect={(event) => {
            event.preventDefault();
            void copy();
          }}
        >
          {status === "copied" ? <Check /> : <Copy />}
          {status === "copied" ? "Copied" : "Copy URL"}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to="/dashboard">Setup guides</Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
