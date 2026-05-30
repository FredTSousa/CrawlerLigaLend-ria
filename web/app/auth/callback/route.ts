import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const code = searchParams.get("code");
  const next = searchParams.get("next") ?? "/";

  // 1. Determine the correct base URL safely
  const baseUrl = process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000";

  if (code) {
    const supabase = createClient();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    
    if (!error) {
      // 2. Redirect using the absolute trusted base URL
      return NextResponse.redirect(new URL(next, baseUrl));
    }
  }

  // Fallback on error
  return NextResponse.redirect(new URL("/login?error=auth", baseUrl));
}
