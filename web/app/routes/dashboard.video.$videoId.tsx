import { ArrowLeft } from "lucide-react";
import { Link } from "react-router";

import type { Route } from "./+types/dashboard.video.$videoId";
import { getEnv } from "~/lib/env.server";
import {
  showTranscript,
  showVideo,
  type TranscriptResource,
  type VideoResource,
} from "~/lib/hosted-api.server";
import { isUnauthorized, requireSessionToken, signupRedirect } from "~/lib/session.server";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardContent } from "~/components/ui/card";
import { YouTubeEmbed } from "~/components/youtube-embed";
import { formatDate } from "~/lib/utils";

const TRANSCRIPT_PAGE_SEGMENTS = 400;

function videoPagePath(videoId: string, startSeconds: number, offset?: number | null): string {
  const searchParams = new URLSearchParams();
  if (startSeconds > 0) searchParams.set("t", String(startSeconds));
  if (typeof offset === "number" && offset > 0) searchParams.set("offset", String(offset));
  const query = searchParams.toString();
  return `/dashboard/video/${encodeURIComponent(videoId)}${query ? `?${query}` : ""}`;
}

export function meta({ data }: Route.MetaArgs) {
  const title = data?.video?.title ?? "Video";
  return [{ title: `${title} · Yutome` }];
}

export async function loader({ request, context, params }: Route.LoaderArgs) {
  const env = getEnv(context);
  const token = requireSessionToken(request);
  const url = new URL(request.url);
  const startSeconds = Math.max(0, Number.parseInt(url.searchParams.get("t") ?? "0", 10) || 0);
  const offset = Math.max(0, Number.parseInt(url.searchParams.get("offset") ?? "0", 10) || 0);
  try {
    const video = await showVideo(env, token, params.videoId);
    let transcript: TranscriptResource | null = null;
    if (video.active_transcript_version_id) {
      transcript = await showTranscript(env, token, video.active_transcript_version_id, {
        offset,
        limit: TRANSCRIPT_PAGE_SEGMENTS,
      });
    }
    return { video, transcript, startSeconds };
  } catch (error) {
    if (isUnauthorized(error)) {
      signupRedirect(env);
    }
    throw error;
  }
}

export default function DashboardVideo({ loaderData }: Route.ComponentProps) {
  const { video, transcript, startSeconds } = loaderData as {
    video: VideoResource;
    transcript: TranscriptResource | null;
    startSeconds: number;
  };
  const embedId = video.youtube_video_id ?? video.video_id;
  const channel = video.channel_title ?? video.channel_handle ?? video.channel_id ?? null;
  const hasPreviousTranscriptPage = (transcript?.offset ?? 0) > 0;
  const hasNextTranscriptPage = typeof transcript?.next_offset === "number";
  const previousTranscriptOffset = transcript
    ? Math.max(0, transcript.offset - TRANSCRIPT_PAGE_SEGMENTS)
    : 0;

  return (
    <div className="grid gap-6">
      <Link to="/dashboard/search" className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-sm">
        <ArrowLeft className="size-4" /> Back to search
      </Link>

      <YouTubeEmbed youtubeVideoId={embedId} startSeconds={startSeconds} title={video.title} />

      <section className="grid gap-1">
        <h1 className="text-xl font-semibold">{video.title ?? video.video_id}</h1>
        <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-sm">
          {channel ? <span>{channel}</span> : null}
          {video.published_at ? <span>· {formatDate(video.published_at)}</span> : null}
          {transcript?.language ? <Badge variant="outline">{transcript.language}</Badge> : null}
        </div>
      </section>

      <section className="grid gap-3">
        <h2 className="text-lg font-semibold">Transcript</h2>
        {!video.active_transcript_version_id ? (
          <p className="text-muted-foreground text-sm">No transcript indexed for this video yet.</p>
        ) : transcript ? (
          <Card>
            <CardContent className="pt-6">
              <pre className="max-h-[32rem] overflow-y-auto text-sm leading-relaxed whitespace-pre-wrap">
                {transcript.text || "Transcript is empty."}
              </pre>
              {hasPreviousTranscriptPage || hasNextTranscriptPage ? (
                <div className="mt-4 flex flex-wrap gap-2">
                  {hasPreviousTranscriptPage ? (
                    <Button asChild size="sm" variant="outline">
                      <Link to={videoPagePath(video.video_id, startSeconds, previousTranscriptOffset)}>Previous</Link>
                    </Button>
                  ) : null}
                  {hasNextTranscriptPage ? (
                    <Button asChild size="sm" variant="secondary">
                      <Link to={videoPagePath(video.video_id, startSeconds, transcript.next_offset)}>Load more</Link>
                    </Button>
                  ) : null}
                </div>
              ) : null}
            </CardContent>
          </Card>
        ) : (
          <p className="text-muted-foreground text-sm">Transcript unavailable.</p>
        )}
      </section>
    </div>
  );
}
