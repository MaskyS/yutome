import { Link } from "react-router";

import type { Route } from "./+types/home";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";

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
    <main className="mx-auto flex min-h-svh max-w-2xl flex-col items-center justify-center gap-8 px-4 py-12 text-center">
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

      <section className="grid w-full gap-4 text-left sm:grid-cols-2">
        <p className="text-muted-foreground text-sm sm:col-span-2 text-center">
          One retrieval model, two front doors — query the same library however your tools work.
        </p>
        <Card>
          <CardHeader>
            <CardTitle>Ask your assistant</CardTitle>
            <CardDescription>
              Paste one MCP URL into Claude, ChatGPT, or any MCP-aware app and ask about your library in
              chat — no scripting required.
            </CardDescription>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Call the HTTP API</CardTitle>
            <CardDescription>
              Hit the same library from a script or agent with a bearer-token HTTP API:{" "}
              <code className="bg-muted rounded px-1 py-0.5 text-xs">find</code>,{" "}
              <code className="bg-muted rounded px-1 py-0.5 text-xs">list</code>,{" "}
              <code className="bg-muted rounded px-1 py-0.5 text-xs">show</code>, and{" "}
              <code className="bg-muted rounded px-1 py-0.5 text-xs">q</code>.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-muted-foreground text-xs">
              Available today when you self-host or run the CLI server.
            </p>
          </CardContent>
        </Card>
      </section>

      <footer className="text-muted-foreground flex gap-4 text-xs">
        <Link to="/privacy" className="hover:text-foreground">
          Privacy
        </Link>
        <Link to="/terms" className="hover:text-foreground">
          Terms
        </Link>
      </footer>
    </main>
  );
}
