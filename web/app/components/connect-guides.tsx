import { useState, type ReactNode } from "react";
import { Braces, Check, Copy, Monitor, MessageSquare, SquareTerminal } from "lucide-react";

import { Button } from "~/components/ui/button";
import { Card, CardContent } from "~/components/ui/card";

// Per-assistant connect instructions, hidden behind a button per client. The
// `icon` slot is a placeholder for each brand's real logo SVG — swap in later.

function Copyable({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative">
      <pre className="bg-muted overflow-x-auto rounded-md p-3 pr-20 font-mono text-xs leading-relaxed break-all whitespace-pre-wrap">
        {text}
      </pre>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        className="absolute top-1.5 right-1.5 h-7 gap-1 px-2 text-xs"
        aria-label="Copy to clipboard"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(text);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          } catch {
            /* clipboard blocked (insecure context) — user can select manually */
          }
        }}
      >
        {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
        {copied ? "Copied" : "Copy"}
      </Button>
    </div>
  );
}

function Steps({ children, start }: { children: ReactNode; start?: number }) {
  return (
    <ol className="text-muted-foreground ml-4 grid list-decimal gap-1.5 text-sm leading-relaxed" start={start}>
      {children}
    </ol>
  );
}

function Note({ children }: { children: ReactNode }) {
  return <p className="text-muted-foreground text-xs leading-relaxed">{children}</p>;
}

interface ClientGuide {
  id: string;
  label: string;
  sub: string;
  icon: ReactNode;
  render: (mcpUrl: string) => ReactNode;
}

const CLIENTS: ClientGuide[] = [
  {
    id: "chatgpt",
    label: "ChatGPT",
    sub: "Developer mode",
    icon: <MessageSquare className="size-5" />,
    render: (mcpUrl) => (
      <div className="grid gap-3">
        <Note>
          Needs a paid plan. Plus/Pro work but are read-only — fine, Yutome is read-only. Custom connectors
          aren&apos;t available to Plus/Pro in the EU, UK, or Switzerland (use Team/Enterprise there).
        </Note>
        <Steps>
          <li>
            <b>Settings → Apps &amp; Connectors → Advanced settings</b>, turn on <b>Developer mode</b>.
          </li>
          <li>
            Back in <b>Apps &amp; Connectors</b>, click <b>Create</b> (Add custom connector).
          </li>
          <li>
            Name it <b>Yutome</b>, set <b>Authentication: OAuth</b>, and paste this MCP server URL:
          </li>
        </Steps>
        <Copyable text={mcpUrl} />
        <Steps start={4}>
          <li>
            Check &ldquo;I trust this application&rdquo; → <b>Create</b>, then approve the OAuth window.
          </li>
          <li>
            In a chat: <b>+</b> → <b>Developer mode</b> (or Tools) → enable <b>Yutome</b>.
          </li>
        </Steps>
      </div>
    ),
  },
  {
    id: "claude-desktop",
    label: "Claude Desktop",
    sub: "& claude.ai",
    icon: <Monitor className="size-5" />,
    render: (mcpUrl) => (
      <div className="grid gap-3">
        <Note>
          Same steps on claude.ai (web). This is the <b>remote custom connector</b> UI — not{" "}
          <code>claude_desktop_config.json</code>, which is only for local servers. On Team/Enterprise an Owner
          must enable custom connectors org-wide first.
        </Note>
        <Steps>
          <li>
            <b>Settings → Connectors → Add custom connector</b>.
          </li>
          <li>
            Name it <b>Yutome</b> and paste the Remote MCP server URL (leave OAuth client ID/secret blank):
          </li>
        </Steps>
        <Copyable text={mcpUrl} />
        <Steps start={3}>
          <li>
            <b>Add</b> → <b>Connect</b>, then approve the OAuth window.
          </li>
          <li>
            In a chat: <b>+</b> or <b>/</b> → <b>Connectors</b> → toggle <b>Yutome</b> on.
          </li>
        </Steps>
      </div>
    ),
  },
  {
    id: "claude-code",
    label: "Claude Code",
    sub: "terminal",
    icon: <SquareTerminal className="size-5" />,
    render: (mcpUrl) => (
      <div className="grid gap-3">
        <Note>Run in your terminal, then approve the browser login.</Note>
        <Copyable text={`claude mcp add --transport http yutome ${mcpUrl}`} />
        <Steps>
          <li>
            Add <code>--scope user</code> to use Yutome in every project (default is just the current one).
          </li>
          <li>
            Run <code>/mcp</code> inside Claude Code and complete the browser OAuth.
          </li>
        </Steps>
      </div>
    ),
  },
  {
    id: "mcp-json",
    label: "mcp.json",
    sub: "Cursor, others",
    icon: <Braces className="size-5" />,
    render: (mcpUrl) => (
      <div className="grid gap-3">
        <Note>
          Most clients (Cursor → <code>~/.cursor/mcp.json</code>, Goose, LibreChat…) accept this; OAuth runs in
          the browser automatically.
        </Note>
        <Copyable text={JSON.stringify({ mcpServers: { yutome: { url: mcpUrl } } }, null, 2)} />
        <Note>
          <b>VS Code</b> (Copilot agent mode) differs — use <code>servers</code> + <code>&quot;type&quot;: &quot;http&quot;</code> in{" "}
          <code>.vscode/mcp.json</code>:
        </Note>
        <Copyable text={JSON.stringify({ servers: { yutome: { type: "http", url: mcpUrl } } }, null, 2)} />
        <Note>For clients that only support local/stdio servers, bridge with mcp-remote:</Note>
        <Copyable
          text={JSON.stringify({ mcpServers: { yutome: { command: "npx", args: ["-y", "mcp-remote", mcpUrl] } } }, null, 2)}
        />
      </div>
    ),
  },
];

export function ConnectGuides({ mcpUrl }: { mcpUrl: string }) {
  const [open, setOpen] = useState<string | null>(null);
  const active = CLIENTS.find((client) => client.id === open);
  return (
    <div className="grid gap-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {CLIENTS.map((client) => (
          <Button
            key={client.id}
            type="button"
            variant={open === client.id ? "default" : "outline"}
            className="h-auto flex-col gap-1 py-3"
            aria-expanded={open === client.id}
            onClick={() => setOpen(open === client.id ? null : client.id)}
          >
            {client.icon}
            <span className="text-sm font-medium">{client.label}</span>
            <span className="text-xs opacity-70">{client.sub}</span>
          </Button>
        ))}
      </div>
      {active ? (
        <Card>
          <CardContent className="pt-6">{active.render(mcpUrl)}</CardContent>
        </Card>
      ) : (
        <p className="text-muted-foreground text-sm">Pick your assistant above for exact steps.</p>
      )}
    </div>
  );
}
