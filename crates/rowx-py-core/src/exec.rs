use std::fmt;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::prelude::*;
use pyo3::types::PyCFunction;
use rowx_core::cancel::{Backend, Hook};
use rowx_core::error::CoreError;
use rowx_core::pool::{Manage, Pool};
use rowx_core::state::{Claim, State};

/// How a call reaches its connection: the lease of a pooled connection (a released one can no longer
/// claim it) and the pool a backend may need to tell about the call's outcome.
pub struct Access<M: Manage> {
    pub lease: Option<u64>,
    pub pool: Option<Pool<M>>,
}

// by hand, so the manager needs none of these itself
impl<M: Manage> Clone for Access<M> {
    fn clone(&self) -> Self {
        Self {
            lease: self.lease,
            pool: self.pool.clone(),
        }
    }
}

impl<M: Manage> Default for Access<M> {
    fn default() -> Self {
        Self {
            lease: None,
            pool: None,
        }
    }
}

impl<M: Manage> fmt::Debug for Access<M> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Access")
            .field("lease", &self.lease)
            .field("pool", &self.pool)
            .finish()
    }
}

impl<M: Manage> Access<M> {
    /// # Errors
    /// `Busy`, `Closed`, `Broken`, or `StaleLease` for a revoked lease.
    pub fn claim(&self, state: &Arc<State>) -> Result<Option<Claim>, CoreError> {
        self.lease
            .map_or_else(|| state.claim(), |lease| state.claim_leased(lease))
    }

    /// A call that arrived while a cancelled operation was settling waits here, outside the GIL.
    ///
    /// # Errors
    /// `Closed`/`Broken` when the connection closed or broke while waiting, or `StaleLease` when the
    /// lease was revoked.
    pub async fn settle_claim(
        &self,
        state: &Arc<State>,
        claim: Option<Claim>,
    ) -> Result<Claim, CoreError> {
        match (claim, self.lease) {
            (Some(claim), _) => Ok(claim),
            (None, Some(lease)) => state.claim_settled_leased(lease).await,
            (None, None) => state.claim_settled().await,
        }
    }

    /// Registers a deferred claim without leaving a window for cancellation to miss it.
    ///
    /// # Errors
    /// The connection could not be claimed, or this operation was cancelled.
    pub async fn reserve(
        &self,
        state: &Arc<State>,
        hook: &Hook<M::Backend>,
    ) -> Result<(), CoreError> {
        while !hook.reserve(|| self.claim(state))? {
            state.wait_settled().await?;
        }
        Ok(())
    }
}

/// Runs `f` when the Python future is cancelled.
///
/// # Errors
/// Any Python error from registering the callback.
pub fn on_cancel(future: &Bound<'_, PyAny>, f: impl Fn() + Send + 'static) -> PyResult<()> {
    let callback = PyCFunction::new_closure(future.py(), None, None, move |args, _kwargs| {
        if let Ok(done) = args.get_item(0)
            && done.call_method0("cancelled")?.is_truthy()?
        {
            f();
        }
        PyResult::Ok(())
    })?;
    future.call_method1("add_done_callback", (callback,))?;
    Ok(())
}

/// asyncio runs done callbacks before the awaiting task resumes, while the Rust future is only dropped
/// later on another thread; settling here keeps `cancel(); await next()` from seeing Busy
///
/// # Errors
/// Any Python error from registering the callback.
pub fn watch<B: Backend>(future: &Bound<'_, PyAny>, hook: Hook<B>) -> PyResult<()> {
    on_cancel(future, move || hook.fire())
}

/// Sets `flag` when the Python future is cancelled. A pool checks it right after acquiring, because
/// the Rust future is dropped late: a cancelled waiter must never run its query.
///
/// # Errors
/// Any Python error from registering the callback.
pub fn watch_flag(future: &Bound<'_, PyAny>, flag: Arc<AtomicBool>) -> PyResult<()> {
    on_cancel(future, move || flag.store(true, Ordering::Release))
}
