import { Link } from "react-router";

import type { Route } from "./+types/terms";

export function meta(_: Route.MetaArgs) {
  return [
    { title: "Terms · Yutome" },
    {
      name: "description",
      content: "Terms for using Yutome hosted YouTube transcript search and assistant connectors.",
    },
  ];
}

export default function Terms() {
  return (
    <main className="mx-auto grid max-w-3xl gap-10 px-4 py-12 sm:py-16">
      <header className="grid gap-3">
        <Link to="/" className="text-muted-foreground text-sm hover:text-foreground">
          Yutome
        </Link>
        <h1 className="text-3xl font-semibold tracking-tight">Terms</h1>
        <p className="text-muted-foreground text-sm">Last updated: May 28, 2026</p>
      </header>

      <section className="grid gap-4 text-sm leading-6">
        <p>
          These terms govern your use of Yutome, a hosted service for importing YouTube sources, indexing transcripts,
          and searching that library from a dashboard or assistant connector.
        </p>
        <h2 className="text-lg font-semibold">Use Of The Service</h2>
        <p>
          You are responsible for the sources you add, the assistant clients you connect, and how you use search
          results. You may not use Yutome to violate law, platform rules, intellectual property rights, privacy rights,
          or security controls.
        </p>
        <h2 className="text-lg font-semibold">YouTube And Google</h2>
        <p>
          If you connect YouTube, you authorize Yutome to use the YouTube Data API to read your subscription list and
          import channels you select. Your use of YouTube features is also subject to the{" "}
          <a className="underline" href="https://www.youtube.com/t/terms">
            YouTube Terms of Service
          </a>
          ,{" "}
          <a className="underline" href="https://policies.google.com/privacy">
            Google Privacy Policy
          </a>
          , and Google API Services User Data Policy.
        </p>
        <h2 className="text-lg font-semibold">Availability</h2>
        <p>
          Yutome is in active development. Hosted features may change, pause, or fail while the service is being
          prepared for broader availability.
        </p>
        <h2 className="text-lg font-semibold">Content And Results</h2>
        <p>
          Transcripts, summaries, search snippets, model outputs, and metadata can be incomplete or inaccurate. Verify
          important information against the original source.
        </p>
        <h2 className="text-lg font-semibold">Termination</h2>
        <p>
          Yutome may suspend or terminate access for abuse, security risk, or violations of these terms. You may stop
          using Yutome and request account deletion at any time.
        </p>
        <h2 className="text-lg font-semibold">Contact</h2>
        <p>
          Questions about these terms can be sent to{" "}
          <a className="underline" href="mailto:contact@maskys.com">contact@maskys.com</a>.
        </p>
      </section>
    </main>
  );
}
