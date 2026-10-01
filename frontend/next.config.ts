import type { NextConfig } from "next";

// The browser only ever talks to Next.js; /api/* is forwarded to the Python backend,
// so there is no CORS to configure and the backend need not be exposed.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // `next dev` trusts only localhost by default; allow colleagues on the same LAN to open
  // it at http://192.168.x.x:3000 or http://10.x.x.x:3000 (office Wi-Fi). Has no effect on
  // `next build` / `next start`.
  allowedDevOrigins: ["192.168.*.*", "10.*.*.*"],
  experimental: {
    // An exact COUNT(*) on the 73M row source table outlives the 30s default.
    proxyTimeout: 330_000,
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${BACKEND_URL}/api/:path*` }];
  },
};

export default nextConfig;
