use std::future::Future;
use std::sync::Arc;

use crate::cancel::Backend;
use crate::state::State;

/// What a backend provides so the pool can create, check, reset and close its connections.
///
/// Connections carry their own [`State`]; the pool drives leases and settling through it, and reuses
/// the cancellation [`Backend`] to stop an operation that is still running when a lease is released.
pub trait Manage: Send + Sync + 'static {
    type Conn: Send + Sync + 'static;
    type Error: std::error::Error + Send + Sync + 'static;
    type Backend: Backend;

    fn connect(&self) -> impl Future<Output = Result<Self::Conn, Self::Error>> + Send;

    fn state<'a>(&self, conn: &'a Self::Conn) -> &'a Arc<State>;

    fn backend(&self, conn: &Self::Conn) -> Self::Backend;

    /// A cheap round trip, run only when a connection has idled longer than `ping_after`.
    fn ping(&self, conn: &Self::Conn) -> impl Future<Output = Result<(), Self::Error>> + Send;

    /// Puts the session back to a clean state; `rollback` is set when it may be inside a transaction.
    fn reset(
        &self,
        conn: &Self::Conn,
        rollback: bool,
    ) -> impl Future<Output = Result<(), Self::Error>> + Send;

    /// `true` when the connection can no longer be used.
    fn is_broken(&self, conn: &Self::Conn) -> bool;

    /// Resolves once a cancelled operation has settled; `false` when the connection did not survive.
    fn settled(&self, conn: &Self::Conn) -> impl Future<Output = bool> + Send;

    /// Drops the per-connection statement cache, for a connection behind the schema epoch.
    fn forget_statements(&self, conn: &Self::Conn) -> impl Future<Output = ()> + Send;

    fn close(&self, conn: Self::Conn) -> impl Future<Output = ()> + Send;
}
