import { ExternalLink, Search } from "lucide-react";
import { Form, Link, useNavigation } from "react-router";

import type { Route } from "./+types/dashboard.search";
import { getEnv } from "~/lib/env.server";
import { searchFind, type SearchMode, type SearchResult } from "~/lib/hosted-api.server";
import { isUnauthorized, requireSessionToken, signupRedirect } from "~/lib/session.server";
import { formatTimestamp } from "~/lib/utils";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardContent } from "~/components/ui/card";
import { Input } from "~/components/ui/input";

export function meta(_: Route.MetaArgs) {
  return [{ title: "Search · Yutome" }];
}

const MODES: SearchMode[] = ["hybrid", "semantic", "lexical"];

function normalizeMode(value: string | null): SearchMode {
  return value && (MODES as string[]).includes(value) ? (value as SearchMode) : "hybrid";
}

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const token = requireSessionToken(request);
  const url = new URL(request.url);
  const query = (url.searchParams.get("q") ?? "").trim();
  const mode = normalizeMode(url.searchParams.get("mode"));
  if (!query) {
    return { query, mode, results: null as SearchResult | null, error: null as string | null };
  }
  try {
    const results = await searchFind(env, token, { text: query, mode, limit: 20 });
    return { query, mode, results, error: null as string | null };
  } catch (error) {
    if (isUnauthorized(error)) {
      signupRedirect(env);
    }
    const message = error instanceof Error ? error.message : "Search failed.";
    return { query, mode, results: null as SearchResult | null, error: message };
  }
}

export default function DashboardSearch({ loaderData }: Route.ComponentProps) {
  const { query, mode, results, error } = loaderData;
  const navigation = useNavigation();
  const busy = navigation.state !== "idle";
  const rows = results?.rows ?? [];

  return (
    <div className="grid gap-6">
      <section className="grid gap-2">
        <h1 className="text-xl font-semibold">Search your library</h1>
        <p className="text-muted-foreground text-sm">
          Search inside transcripts across every channel you&apos;ve indexed. Each hit links to the moment in the video.
        </p>
      </section>

      <Form method="get" className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[16rem] flex-1">
          <Search className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2" />
          <Input
            name="q"
            defaultValue={query}
            placeholder="e.g. gut motility, senolytics, NAD+"
            autoComplete="off"
            autoFocus
            className="pl-9"
          />
        </div>
        <select
          name="mode"
          defaultValue={mode}
          className="border-input bg-background h-9 rounded-md border px-3 text-sm"
          aria-label="Search mode"
        >
          {MODES.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <Button type="submit" disabled={busy}>
          {busy ? "Searching…" : "Search"}
        </Button>
      </Form>

      {error ? (
        <Card>
          <CardContent className="text-destructive pt-6 text-sm">{error}</CardContent>
        </Card>
      ) : null}

      {!query ? (
        <p className="text-muted-foreground text-sm">Enter a query to search your transcripts.</p>
      ) : !error && rows.length === 0 ? (
        <p className="text-muted-foreground text-sm">No matches for &ldquo;{query}&rdquo;.</p>
      ) : (
        <section className="grid gap-3">
          {rows.map((hit) => {
            const startSeconds = Math.floor((hit.start_ms ?? 0) / 1000);
            const channel = hit.channel_title ?? hit.channel_handle ?? hit.channel_id ?? "Unknown channel";
            return (
              <Card key={hit.chunk_id}>
                <CardContent className="grid gap-2 pt-6">
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-medium">{hit.title ?? hit.video_id}</span>
                    <span className="text-muted-foreground">· {channel}</span>
                    <Badge variant="secondary">{formatTimestamp(hit.start_ms)}</Badge>
                    {hit.match_type ? <Badge variant="outline">{hit.match_type}</Badge> : null}
                  </div>
                  {hit.snippet ? <p className="text-muted-foreground text-sm">{hit.snippet}</p> : null}
                  <div className="flex flex-wrap items-center gap-3 text-sm">
                    <Button asChild size="sm" variant="secondary">
                      <Link to={`/dashboard/video/${encodeURIComponent(hit.video_id)}?t=${startSeconds}`}>
                        Read &amp; play
                      </Link>
                    </Button>
                    <a
                      className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
                      href={hit.youtube_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open on YouTube <ExternalLink className="size-3.5" />
                    </a>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </section>
      )}
    </div>
  );
}
