import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "shillscore — crypto-Twitter signal accuracy",
  description: "Track which Twitter accounts actually call winners.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="bg-bg text-ink min-h-screen antialiased">{children}</body>
    </html>
  );
}
