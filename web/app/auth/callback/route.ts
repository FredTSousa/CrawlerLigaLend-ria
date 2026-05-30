import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const code = searchParams.get("code");
  const next = searchParams.get("next") ?? "/";

  // 1. Dynamically find the correct deployment URL
  let baseUrl = process.env.NEXT_PUBLIC_SITE_URL;

  // If running on Vercel preview branches, use the system-assigned URL
  if (process.env.NEXT_PUBLIC_VERCEL_URL) {
    baseUrl = `https://${process.env.NEXT_PUBLIC_VERCEL_URL}`;
  }

  // Fallback for local development if neither env variable is set
  if (!baseUrl) {
    baseUrl = "http://localhost:3000";
  }

  if (code) {
    const supabase = createClient();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    
    if (!error) {
      return NextResponse.redirect(new URL(next, baseUrl));
    }
  }

  // Fallback on error
  return NextResponse.redirect(new URL("/login?error=auth", baseUrl));
}
