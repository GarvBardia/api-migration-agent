/** @type {import('next').NextConfig} */

// Static export for the public GitHub Pages demo (added 2026-09-20, see
// PROJECT_STATUS.md). `output: "export"` is unconditional (not just for
// production builds) on purpose: it makes `next dev` enforce the same
// constraints a real export does, so an export-incompatible change (e.g. a
// dynamic route with no generateStaticParams) fails locally instead of only
// surfacing at deploy time.
//
// basePath: GitHub Pages serves a *project* site under /<repo-name>/, so
// without it every /_next/static asset URL points at the domain root and
// 404s -- a page that "deploys successfully" but renders blank. Read from
// NEXT_PUBLIC_BASE_PATH at build time; empty (the default) for local dev.
//
// trailingSlash: emit /repos/index.html rather than /repos.html -- the
// layout every static host resolves without special extensionless-URL
// handling.
const nextConfig = {
  output: "export",
  trailingSlash: true,
  basePath: process.env.NEXT_PUBLIC_BASE_PATH || "",
};

export default nextConfig;
