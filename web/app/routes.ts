import { type RouteConfig, index, route } from "@react-router/dev/routes";

export default [
  index("routes/home.tsx"),
  route("cli/authorize", "routes/cli.authorize.tsx"),
  route("signup", "routes/signup.tsx"),
  route("signout", "routes/signout.tsx"),
  route("dashboard", "routes/dashboard.tsx", [
    index("routes/dashboard.home.tsx"),
    route("search", "routes/dashboard.search.tsx"),
    route("video/:videoId", "routes/dashboard.video.$videoId.tsx"),
    route("connect", "routes/dashboard.connect.tsx"),
  ]),
] satisfies RouteConfig;
