use std::sync::Arc;
use std::time::Duration;

use tokio::time::Instant;

use super::{Inner, Manage, Parts, Slot};
use crate::cancel::Backend;

// how often an abandoned operation is asked again to stop, as in `cancel.rs`
const RECANCEL_EVERY: Duration = Duration::from_millis(100);

impl<M: Manage> Inner<M> {
    // the release path: revoke the lease, settle, reset, then idle or close; the slot is held until then
    pub(super) async fn release(self: &Arc<Self>, mut parts: Parts<M>, slot: Slot) {
        self.unregister(parts.meta.id);
        let usable = self.settle_and_reset(&parts.conn).await;
        parts.meta.queries += 1;
        let now = Instant::now();
        if usable && !self.retired(&parts.meta, now) && !self.closed() {
            parts.meta.idle_since = now;
            if let Err(parts) = self.push_idle(parts) {
                self.discard(parts.conn).await;
            }
        } else {
            self.discard(parts.conn).await;
        }
        drop(slot);
    }

    // `false` when the connection must not return to service
    async fn settle_and_reset(&self, conn: &M::Conn) -> bool {
        let state = self.manager.state(conn);
        // revoked first, so nothing built on the old lease can claim the connection again
        state.renew_lease();
        if self.manager.is_broken(conn) {
            return false;
        }
        if state.is_busy() {
            // an operation or stream still holds the connection. It must end before the reset takes
            // over, or it could run SQL afterwards and its claim drop would clear a later borrower's
            // Busy state; a cancel request only stops a statement that is running, so it is resent
            state.mark_maybe_in_txn();
            let backend = self.manager.backend(conn);
            while tokio::time::timeout(RECANCEL_EVERY, state.wait_unclaimed())
                .await
                .is_err()
            {
                // a failed request is fine: the next round tries again
                let _ = backend.send_cancel().await;
            }
        }
        // a cancelled operation settles on its own (Settling → Idle) before the reset may run
        if !self.manager.settled(conn).await {
            return false;
        }
        let rollback = state.maybe_in_txn();
        let result = self.manager.reset(conn, rollback).await;
        let ok = result.is_ok() && !self.manager.is_broken(conn);
        if ok {
            state.clear_maybe_in_txn();
        }
        ok
    }
}
