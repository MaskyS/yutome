import { Link } from "react-router";

import type { Route } from "./+types/privacy";

export function meta(_: Route.MetaArgs) {
  return [
    { title: "Privacy Policy · Yutome" },
    {
      name: "description",
      content: "Privacy policy for Yutome account sign-in, YouTube connection, and assistant search.",
    },
  ];
}

export default function Privacy() {
  return (
    <main className="mx-auto grid max-w-3xl gap-10 px-4 py-12 sm:py-16">
      <header className="grid gap-3">
        <Link to="/" className="text-muted-foreground text-sm hover:text-foreground">
          Yutome
        </Link>
        <h1 className="text-3xl font-semibold tracking-tight">Privacy Policy</h1>
        <p className="text-muted-foreground text-sm">Last updated: May 28, 2026</p>
      </header>

      <section className="grid gap-4 text-sm leading-6">
        <p>
          Yutome helps you build and search a personal YouTube transcript library. This policy explains what data
          Yutome collects, how it is used, and how you can disconnect or delete it.
        </p>
        <h2 className="text-lg font-semibold">Data We Collect</h2>
        <p>
          When you sign in, Yutome stores account information such as your email address, name, workspace, session
          records, connected assistant records, and usage or job activity needed to operate the service.
        </p>
        <p>
          If you choose to connect YouTube, Yutome requests the read-only YouTube Data API scope
          <code className="bg-muted mx-1 rounded px-1 py-0.5">https://www.googleapis.com/auth/youtube.readonly</code>.
          Yutome uses that scope to read your YouTube subscription list so you can choose channels to import. After
          import, selected channels are handled as public YouTube sources.
        </p>
        <p>
          Yutome may store imported channel, playlist, video, transcript, search index, and job metadata so your
          workspace can search and refresh the sources you selected.
        </p>
        <h2 className="text-lg font-semibold">How We Use Google Data</h2>
        <p>
          Google sign-in data is used only to authenticate your Yutome account. YouTube data is used only to discover
          your subscriptions, show them in the dashboard, and import the channels you select. Yutome does not sell
          Google user data or use it for advertising.
        </p>
        <p>
          Yutome&apos;s use and transfer of information received from Google APIs adheres to the{" "}
          <a className="underline" href="https://developers.google.com/terms/api-services-user-data-policy">
            Google API Services User Data Policy
          </a>
          , including the Limited Use requirements.
        </p>
        <h2 className="text-lg font-semibold">Sharing</h2>
        <p>
          Yutome shares data with infrastructure providers only as needed to run the service, including hosting,
          database, email, and observability providers. Yutome also sends requests to YouTube and configured model or
          search providers when needed to import, index, or search your selected sources.
        </p>
        <h2 className="text-lg font-semibold">Security</h2>
        <p>
          Yutome uses HTTPS, signed sessions, scoped service credentials, and encrypted storage for YouTube OAuth token
          material. Access to production systems is limited to operators who need it to maintain the service.
        </p>
        <h2 className="text-lg font-semibold">Disconnecting And Deleting</h2>
        <p>
          You can disconnect YouTube in the Yutome dashboard or revoke Yutome from your{" "}
          <a className="underline" href="https://myaccount.google.com/permissions">
            Google Account permissions
          </a>
          . To request workspace or account deletion, email <a className="underline" href="mailto:contact@maskys.com">contact@maskys.com</a>.
        </p>
        <h2 className="text-lg font-semibold">Contact</h2>
        <p>
          Questions about this policy can be sent to{" "}
          <a className="underline" href="mailto:contact@maskys.com">contact@maskys.com</a>.
        </p>
      </section>
    </main>
  );
}
