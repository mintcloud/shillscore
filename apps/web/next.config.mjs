/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  async rewrites() {
    const internal = process.env.INTERNAL_API_URL ?? "http://api:8000";
    return [
      { source: "/api/:path*", destination: `${internal}/api/:path*` },
      { source: "/auth/:path*", destination: `${internal}/auth/:path*` },
    ];
  },
};

export default nextConfig;
