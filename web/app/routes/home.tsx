import { Link } from "react-router";

import type { Route } from "./+types/home";
import { Button } from "~/components/ui/button";

export function meta(_: Route.MetaArgs) {
  return [
    { title: "Yutome — your YouTube library, in your assistant" },
    {
      name: "description",
      content: "Index the YouTube channels you care about and search their transcripts from Claude or ChatGPT.",
    },
  ];
}

export default function Home() {
  return (
    <main className="mx-auto flex min-h-svh max-w-2xl flex-col items-center justify-center gap-6 px-4 text-center">
      <h1 className="text-4xl font-semibold tracking-tight">Yutome</h1>
      <p className="text-muted-foreground text-lg">
        Index the YouTube channels you care about and search their transcripts from Claude, ChatGPT, or any
        MCP assistant.
      </p>
      <div className="flex gap-3">
        <Button asChild size="lg">
          <Link to="/signup">Get started</Link>
        </Button>
      </div>
    </main>
  );
}
