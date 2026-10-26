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
        // Busy → Settling: an operation (or stream) is still running and must be stopped
        let abandoned = state.settle();
        if !abandoned && !self.manager.settled(conn).await {
            return false;
        }
        let rollback = state.maybe_in_txn();
        let backend = self.manager.backend(conn);
        let reset = self.manager.reset(conn, rollback);
        tokio::pin!(reset);
        // an abandoned operation holds the connection until it ends, which the reset waits for
        let result = loop {
            match tokio::time::timeout(RECANCEL_EVERY, &mut reset).await {
                Ok(result) => break result,
                Err(_) => {
                    if abandoned {
                        // a failed request is fine: the next round or the reset will notice
                        let _ = backend.send_cancel().await;
                    }
                }
            }
        };
        let ok = result.is_ok() && !self.manager.is_broken(conn);
        if ok {
            state.clear_maybe_in_txn();
        }
        if abandoned {
            state.settled(ok);
        }
        ok
    }
}
