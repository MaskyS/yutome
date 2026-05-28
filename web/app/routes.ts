import { type RouteConfig, index, route } from "@react-router/dev/routes";

export default [
  index("routes/home.tsx"),
  route("cli/authorize", "routes/cli.authorize.tsx"),
  route("signup", "routes/signup.tsx"),
  route("auth/verify", "routes/auth.verify.tsx"),
  route("auth/google/start", "routes/auth.google.start.tsx"),
  route("auth/google/callback", "routes/auth.google.callback.tsx"),
  route("google62668ff7e7299252.html", "routes/google-search-console-verification.ts"),
  route("privacy", "routes/privacy.tsx"),
  route("terms", "routes/terms.tsx"),
  route("signout", "routes/signout.tsx"),
  route("dashboard", "routes/dashboard.tsx", [
    index("routes/dashboard.home.tsx"),
    route("connect", "routes/dashboard.connect.tsx"),
    route("youtube/start", "routes/dashboard.youtube.start.tsx"),
    route("youtube/callback", "routes/dashboard.youtube.callback.tsx"),
    route("search", "routes/dashboard.search.tsx"),
    route("library", "routes/dashboard.library.tsx"),
    route("channel/:channelId", "routes/dashboard.channel.$channelId.tsx"),
    route("video/:videoId", "routes/dashboard.video.$videoId.tsx"),
  ]),
] satisfies RouteConfig;
