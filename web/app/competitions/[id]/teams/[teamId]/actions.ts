"use server";

import { revalidatePath } from "next/cache";
import { createClient } from "@/lib/supabase/server";

export async function setPlayerDeparted(
  competitionId: string,
  teamId: string,
  playerId: string,
  departed: boolean,
) {
  const supabase = createClient();
  const now = new Date().toISOString();
  const { error } = await supabase
    .from("roster_memberships")
    .update({
      active: !departed,
      left_at: departed ? now : null,
      updated_at: now,
    })
    .eq("competition_id", competitionId)
    .eq("team_id", teamId)
    .eq("player_id", playerId);
  if (error) throw new Error(error.message);
  revalidatePath(`/competitions/${competitionId}/teams/${teamId}`);
}
