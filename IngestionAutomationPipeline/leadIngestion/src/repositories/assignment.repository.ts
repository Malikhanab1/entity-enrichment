import { db } from "../db/database";
import { SalesRole } from "../types";

const getStmt = db.prepare(`SELECT next_index FROM assignment_counters WHERE role = ?`);
const upsertStmt = db.prepare(`
  INSERT INTO assignment_counters (role, next_index) VALUES (@role, @nextIndex)
  ON CONFLICT(role) DO UPDATE SET next_index = @nextIndex
`);

/**
 * Atomically reads-and-advances the round-robin pointer for a role.
 * Wrapped in a transaction so concurrent requests never hand out the
 * same index twice.
 */
export const takeNextIndex = db.transaction((role: SalesRole, poolSize: number): number => {
  const row = getStmt.get(role) as { next_index: number } | undefined;
  const current = (row?.next_index ?? 0) % poolSize;
  upsertStmt.run({ role, nextIndex: (current + 1) % poolSize });
  return current;
});
