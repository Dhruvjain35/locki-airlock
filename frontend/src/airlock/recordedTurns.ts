/** The Recorded Voice Input list: newest first, at most 12, and every turn with its own stable key, so a turn never takes over the
 *  view (and investigation record) of the turn that was in its slot before. */
export const MAX_TURNS = 12;

export function addTurn<T extends object>(prev: (T & { key: string })[], turn: T, key: string): (T & { key: string })[] {
  return [{ ...turn, key }, ...prev.filter(t => t.key !== key)].slice(0, MAX_TURNS);
}
