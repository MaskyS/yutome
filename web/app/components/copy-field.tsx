import { useState } from "react";

import { Button } from "~/components/ui/button";
import { Input } from "~/components/ui/input";

export function CopyField({ value }: { value: string }) {
  const [status, setStatus] = useState<"idle" | "copied" | "blocked">("idle");

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setStatus("copied");
      setTimeout(() => setStatus("idle"), 1500);
    } catch {
      // The Clipboard API is unavailable/blocked (e.g. an insecure http context).
      // Surface it instead of failing silently; the field selects on focus so the
      // user can still copy manually.
      setStatus("blocked");
      setTimeout(() => setStatus("idle"), 5000);
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex gap-2">
        <Input
          readOnly
          value={value}
          // min-w-0 lets the long mono URL shrink instead of forcing horizontal
          // page overflow on narrow (mobile) viewports.
          className="min-w-0 font-mono"
          onFocus={(event) => event.currentTarget.select()}
          aria-label="MCP connector URL"
        />
        <Button type="button" variant="secondary" onClick={() => void copy()}>
          {status === "copied" ? "Copied" : "Copy"}
        </Button>
      </div>
      {status === "blocked" ? (
        <p className="text-muted-foreground text-xs" role="status">
          Copy was blocked by your browser — click the field and press ⌘C / Ctrl+C.
        </p>
      ) : null}
    </div>
  );
}
