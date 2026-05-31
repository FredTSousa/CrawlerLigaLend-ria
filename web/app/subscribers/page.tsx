import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import Subscribers from "@/components/Subscribers";

export const dynamic = "force-dynamic";

// Manage external sites subscribed to a league + monitor the delivery outbox.
// See docs/SUBSCRIPTIONS.md for the end-to-end design.
export default async function SubscribersPage() {
  const supabase = createClient();

  const [{ data: subs }, { data: comps }, { data: outbox }] = await Promise.all([
    supabase
      .from("subscribers")
      .select("id,label,competition_id,callback_url,active,created_at")
      .order("created_at", { ascending: false }),
    supabase.from("competitions").select("id,name").order("name"),
    supabase
      .from("delivery_outbox")
      .select("id,match_id,competition_id,status,attempts,last_error,updated_at")
      .order("id", { ascending: false })
      .limit(200),
  ]);

  return (
    <>
      <div className="panel">
        <h2 style={{ margin: 0 }}>Subscribers</h2>
        <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
          External sites that receive a signed webhook on every match change in
          a league. Each gets a snapshot (match + players + reporter ratings).
          See <code>docs/SUBSCRIPTIONS.md</code> for setup on the subscriber side.
        </p>
      </div>

      <Subscribers
        subscribers={(subs ?? []) as any[]}
        competitions={(comps ?? []) as any[]}
        outbox={(outbox ?? []) as any[]}
      />

      <p>
        <Link href="/">← Dashboard</Link>
      </p>
    </>
  );
}
