import "./globals.css";
import type { Metadata } from "next";
import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import SignOut from "@/components/SignOut";

export const metadata: Metadata = {
  title: "Crawler LLP",
  description: "Liga Portugal stats — monitor & trigger crawls",
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const supabase = createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  return (
    <html lang="en">
      <body>
        {user && (
          <header className="site">
            <div className="inner">
              <Link href="/" className="brand">⚽ Crawler LLP</Link>
              <nav>
                <Link href="/">Dashboard</Link>
                <Link href="/runs">Crawl runs</Link>
                <Link href="/reporter-runs">Reporter runs</Link>
              </nav>
              <span className="spacer" />
              <span className="muted" style={{ fontSize: 13 }}>{user.email}</span>
              <SignOut />
            </div>
          </header>
        )}
        <main className="container">{children}</main>
      </body>
    </html>
  );
}
