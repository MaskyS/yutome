import * as React from "react";

import { cn } from "~/lib/utils";

/** Minimal usage meter: a filled bar from 0–100. No external dependency. */
function Progress({
  value,
  className,
  ...props
}: React.ComponentProps<"div"> & { value?: number | null }) {
  const pct = Math.max(0, Math.min(100, value ?? 0));
  return (
    <div
      data-slot="progress"
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      className={cn("bg-muted relative h-2 w-full overflow-hidden rounded-full", className)}
      {...props}
    >
      <div className="bg-primary h-full rounded-full transition-all" style={{ width: `${pct}%` }} />
    </div>
  );
}

export { Progress };
