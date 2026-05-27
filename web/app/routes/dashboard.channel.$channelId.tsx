import { ArrowLeft } from "lucide-react";
import { Link } from "react-router";

import type { Route } from "./+types/dashboard.channel.$channelId";
import { getEnv } from "~/lib/env.server";
import {
  HostedApiError,
  listChannels,
  listVideos,
  showChannel,
  type ChannelListItem,
  type ChannelResource,
} from "~/lib/hosted-api.server";
import { isUnauthorized, requireSessionToken, signupRedirect } from "~/lib/session.server";
import { formatDate } from "~/lib/utils";
import { Button } from "~/components/ui/button";
import { Card, CardContent } from "~/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "~/components/ui/table";

const VIDEO_PAGE = 15;
const MAX_LIMIT = 200;

export function meta({ data }: Route.MetaArgs) {
  const title = data?.header?.title ?? "Channel";
  return [{ title: `${title} · Yutome` }];
}

function clampOffset(raw: string | null): number {
  const parsed = Number.parseInt(raw ?? "0", 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function channelLink(channelId: string, voffset: number): string {
  const params = new URLSearchParams();
  if (voffset > 0) params.set("voffset", String(voffset));
  const query = params.toString();
  return `/dashboard/channel/${encodeURIComponent(channelId)}${query ? `?${query}` : ""}`;
}

export async function loader({ request, context, params }: Route.LoaderArgs) {
  const env = getEnv(context);
  const token = requireSessionToken(request);
  const url = new URL(request.url);
  const voffset = clampOffset(url.searchParams.get("voffset"));
  const channelId = params.channelId;

  // `show channel` is built from the videos table, so a source-only channel
  // (registered, nothing indexed yet) 404s here even though it's a real library
  // channel. Fall back to the channel-list row for its metadata in that case.
  let channel: ChannelResource | null = null;
  try {
    channel = await showChannel(env, token, channelId);
  } catch (error) {
    if (isUnauthorized(error)) signupRedirect(env);
    if (!(error instanceof HostedApiError)) throw error;
  }

  let fallback: ChannelListItem | null = null;
  if (!channel) {
    try {
      fallback = (await listChannels(env, token, { channel: channelId, limit: 1 }))[0] ?? null;
    } catch (error) {
      if (isUnauthorized(error)) signupRedirect(env);
      throw error;
    }
  }

  let videos;
  try {
    videos = await listVideos(env, token, {
      channel: channelId,
      order_by: "newest",
      limit: Math.min(VIDEO_PAGE + 1, MAX_LIMIT),
      offset: voffset,
    });
  } catch (error) {
    if (isUnauthorized(error)) signupRedirect(env);
    throw error;
  }

  const header = channel ?? fallback;
  return {
    channelId,
    header,
    videos: videos.slice(0, VIDEO_PAGE),
    voffset,
    hasNext: videos.length > VIDEO_PAGE,
    notFound: !header && videos.length === 0 && voffset === 0,
  };
}

export default function DashboardChannel({ loaderData }: Route.ComponentProps) {
  const { channelId, header, videos, voffset, hasNext, notFound } = loaderData;

  return (
    <div className="grid gap-6">
      <Link
        to="/dashboard/library"
        className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-sm"
      >
        <ArrowLeft className="size-4" /> Back to library
      </Link>

      {notFound ? (
        <Card>
          <CardContent className="text-muted-foreground pt-6 text-sm">
            We couldn&apos;t find an indexed channel for this id yet.
          </CardContent>
        </Card>
      ) : (
        <>
          <section className="grid gap-1">
            <h1 className="text-xl font-semibold">{header?.title ?? header?.channel_handle ?? channelId}</h1>
            <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-sm">
              {header?.channel_handle ? <span>{header.channel_handle}</span> : null}
              {header?.video_count != null ? <span>· {header.video_count.toLocaleString()} videos</span> : null}
              {header?.latest_published_at ? <span>· latest {formatDate(header.latest_published_at)}</span> : null}
            </div>
          </section>

          <section className="grid gap-3">
            <h2 className="text-lg font-semibold">Videos</h2>
            {videos.length ? (
              <>
                <Card>
                  <CardContent className="pt-6">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Title</TableHead>
                          <TableHead className="text-right">Published</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {videos.map((video) => (
                          <TableRow key={video.video_id}>
                            <TableCell className="font-medium">
                              <Link
                                to={`/dashboard/video/${encodeURIComponent(video.video_id)}`}
                                className="hover:underline"
                              >
                                {video.title ?? video.video_id}
                              </Link>
                            </TableCell>
                            <TableCell className="text-muted-foreground text-right">
                              {video.published_at ? formatDate(video.published_at) : "—"}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </CardContent>
                </Card>
                {voffset > 0 || hasNext ? (
                  <div className="flex items-center gap-2">
                    {voffset > 0 ? (
                      <Button asChild size="sm" variant="outline">
                        <Link to={channelLink(channelId, Math.max(0, voffset - VIDEO_PAGE))}>Previous</Link>
                      </Button>
                    ) : null}
                    {hasNext ? (
                      <Button asChild size="sm" variant="outline">
                        <Link to={channelLink(channelId, voffset + VIDEO_PAGE)}>Next</Link>
                      </Button>
                    ) : null}
                  </div>
                ) : null}
              </>
            ) : (
              <p className="text-muted-foreground text-sm">No videos indexed for this channel yet.</p>
            )}
          </section>
        </>
      )}
    </div>
  );
}
