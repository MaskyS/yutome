export function loader() {
  return new Response("google-site-verification: google62668ff7e7299252.html\n", {
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "public, max-age=3600",
    },
  });
}
