import { useState } from "react";

import { Button } from "~/components/ui/button";
import { Input } from "~/components/ui/input";

export function CopyField({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex gap-2">
      <Input
        readOnly
        value={value}
        className="font-mono"
        onFocus={(event) => event.currentTarget.select()}
        aria-label="MCP connector URL"
      />
      <Button
        type="button"
        variant="secondary"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          } catch {
            // Clipboard can be blocked (e.g. insecure context); selecting the
            // field still lets the user copy manually.
          }
        }}
      >
        {copied ? "Copied" : "Copy"}
      </Button>
    </div>
  );
}
