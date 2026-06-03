import { createClient, type SupabaseClient } from "@supabase/supabase-js";

// Service-role client (bypasses RLS). Used only for server-to-server access
// authenticated by a bearer token (see the export route). Lazy-initialized so a
// build without env vars doesn't throw at import time.
let _admin: SupabaseClient | null = null;

export function createServiceClient(): SupabaseClient {
  if (!_admin) {
    const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
    const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
    if (!url || !key) throw new Error("Supabase service-role env vars are not set");
    _admin = createClient(url, key, {
      auth: { autoRefreshToken: false, persistSession: false },
    });
  }
  return _admin;
}
