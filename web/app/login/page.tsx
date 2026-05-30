"use client";

import { useState } from "react";
import { createClient } from "@/lib/supabase/client";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function sendLink(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    const supabase = createClient();
    const { error } = await supabase.auth.signInWithOtp({
      email,
      options: { redirectTo: `${window.location.origin}/auth/callback` },
    });
    setLoading(false);
    if (error) setError(error.message);
    else setSent(true);
  }

  return (
    <div className="login-card panel">
      <h2>Crawler LLP — sign in</h2>
      {sent ? (
        <p>
          Check <strong>{email}</strong> for a magic link. Open it on this
          device to sign in.
        </p>
      ) : (
        <form onSubmit={sendLink} className="row" style={{ flexDirection: "column", alignItems: "stretch" }}>
          <input
            type="email"
            required
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <button className="btn" disabled={loading} type="submit">
            {loading ? "Sending…" : "Send magic link"}
          </button>
          {error && <p className="tag-red">{error}</p>}
          <p className="muted" style={{ fontSize: 13 }}>
            Access is restricted to approved accounts.
          </p>
        </form>
      )}
    </div>
  );
}
