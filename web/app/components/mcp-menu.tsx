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
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(mcpUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard can be blocked (insecure context); the field is selectable instead.
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
        <DropdownMenuItem
          onSelect={(event) => {
            event.preventDefault();
            void copy();
          }}
        >
          {copied ? <Check /> : <Copy />}
          {copied ? "Copied" : "Copy URL"}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to="/dashboard">Setup guides</Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
