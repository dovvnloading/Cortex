/**
 * Persist a rapidly changing preference in the order the user chose it.
 *
 * A single request per entity may be in flight at once. Subsequent intentions
 * update the desired value and are coalesced into the next write after the
 * current response, so an old response can never commit after a newer one.
 * Unrelated entity keys still proceed independently.
 */
export interface LatestWrite<T> {
  desired: T;
  confirmed: T;
}

export function persistLatestWrite<T>(
  pending: Map<string, LatestWrite<T>>,
  key: string,
  confirmed: T,
  desired: T,
  persist: (value: T) => Promise<unknown>,
  onFailure: (failed: T, confirmed: T, error: unknown) => void,
): void {
  const active = pending.get(key);
  if (active) {
    active.desired = desired;
    return;
  }

  const task: LatestWrite<T> = { desired, confirmed };
  pending.set(key, task);

  void (async () => {
    // An earlier request may have failed after committing server-side. If a
    // newer intention exists, write it even when it matches our last known
    // confirmed value, so the newest intent is sent *after* that earlier one.
    let needsWrite = true;
    try {
      while (needsWrite || !Object.is(task.desired, task.confirmed)) {
        const sending = task.desired;
        try {
          await persist(sending);
          task.confirmed = sending;
          needsWrite = false;
        } catch (error) {
          if (Object.is(task.desired, sending)) {
            onFailure(sending, task.confirmed, error);
            return;
          }
          needsWrite = true;
        }
      }
    } finally {
      // Do not leave a rejected key stuck in a permanently busy state.
      if (pending.get(key) === task) pending.delete(key);
    }
  })();
}
