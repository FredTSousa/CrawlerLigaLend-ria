"use server";

import { revalidatePath } from "next/cache";
import { createClient } from "@/lib/supabase/server";

const CODE_TO_GROUP: Record<string, string> = {
  GR: "Guarda Redes",
  DC: "Defesa", DL: "Defesa",
  MC: "Médio", MA: "Médio",
  AV: "Avançado",
};

export async function savePlayerPosition(
  playerId: string,
  position: string,
  positionCode: string,
) {
  const supabase = createClient();
  const now = new Date().toISOString();
  const positionGroup = CODE_TO_GROUP[positionCode] ?? null;

  const { error } = await supabase
    .from("players")
    .update({ position, position_code: positionCode, position_group: positionGroup, updated_at: now })
    .eq("id", playerId);

  if (error) throw new Error(error.message);

  // Also update any active roster_memberships so the export picks it up.
  if (positionGroup) {
    await supabase
      .from("roster_memberships")
      .update({ position_group: positionGroup, updated_at: now })
      .eq("player_id", playerId);
  }

  revalidatePath(`/players/${playerId}`);
}
