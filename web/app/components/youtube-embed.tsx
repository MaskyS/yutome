interface YouTubeEmbedProps {
  youtubeVideoId: string;
  startSeconds?: number;
  title?: string;
}

/** Privacy-friendly YouTube iframe, seekable to a start offset. */
export function YouTubeEmbed({ youtubeVideoId, startSeconds, title }: YouTubeEmbedProps) {
  const start = Math.max(0, Math.floor(startSeconds ?? 0));
  const src = `https://www.youtube-nocookie.com/embed/${encodeURIComponent(youtubeVideoId)}?start=${start}`;
  return (
    <div className="aspect-video w-full overflow-hidden rounded-lg border bg-black">
      <iframe
        className="h-full w-full"
        src={src}
        title={title ?? "YouTube video"}
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
        allowFullScreen
        loading="lazy"
        referrerPolicy="strict-origin-when-cross-origin"
      />
    </div>
  );
}
