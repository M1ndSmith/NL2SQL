import type { NextConfig } from "next"
import path from "path"

const nextConfig: NextConfig = {
  reactStrictMode: true,
  agentRules: false,
  devIndicators: false,
  allowedDevOrigins: ["127.0.0.1"],
  turbopack: {
    root: path.join(__dirname),
  },
  async rewrites() {
    const api = (process.env.NL2SQL_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "")
    return [{ source: "/v1/:path*", destination: `${api}/v1/:path*` }]
  },
}

export default nextConfig
