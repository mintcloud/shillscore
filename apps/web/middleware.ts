// Drop at: apps/web/middleware.ts
// HTTP Basic auth gate on /admin/* — credentials in ADMIN_USER + ADMIN_PASSWORD.

import { NextResponse, type NextRequest } from "next/server";

export const config = {
  matcher: ["/admin/:path*"],
};

export function middleware(req: NextRequest) {
  const user = process.env.ADMIN_USER;
  const pass = process.env.ADMIN_PASSWORD;

  if (!user || !pass) {
    return new NextResponse("admin disabled: ADMIN_USER/ADMIN_PASSWORD not set", { status: 503 });
  }

  const header = req.headers.get("authorization") ?? "";
  if (header.startsWith("Basic ")) {
    const decoded = atob(header.slice(6));
    const idx = decoded.indexOf(":");
    if (idx > -1) {
      const u = decoded.slice(0, idx);
      const p = decoded.slice(idx + 1);
      if (u === user && p === pass) return NextResponse.next();
    }
  }

  return new NextResponse("auth required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="shillscore-admin"' },
  });
}
