use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering};

use crate::error::Error;

const IDLE: u8 = 0;
const BUSY: u8 = 1;
const CLOSED: u8 = 2;

// minimal state machine: Settling and Broken land with cancellation (design §7.5, §7.6)
#[derive(Debug, Default)]
pub struct State(AtomicU8);

impl State {
    /// Idle → Busy by compare-and-swap; the returned guard moves Busy → Idle when dropped.
    ///
    /// # Errors
    /// `Busy` when another operation holds the connection, `Closed` after `close()`.
    pub fn claim(self: &Arc<Self>) -> Result<Claim, Error> {
        match self
            .0
            .compare_exchange(IDLE, BUSY, Ordering::AcqRel, Ordering::Acquire)
        {
            Ok(_) => Ok(Claim(Arc::clone(self))),
            Err(BUSY) => Err(Error::Busy),
            Err(_) => Err(Error::Closed),
        }
    }

    pub fn close(&self) {
        self.0.store(CLOSED, Ordering::Release);
    }

    #[must_use]
    pub fn is_closed(&self) -> bool {
        self.0.load(Ordering::Acquire) == CLOSED
    }

    #[must_use]
    pub fn is_busy(&self) -> bool {
        self.0.load(Ordering::Acquire) == BUSY
    }
}

#[derive(Debug)]
pub struct Claim(Arc<State>);

impl Drop for Claim {
    fn drop(&mut self) {
        // a failed swap means the connection was closed meanwhile, which must stay closed
        let _ = self
            .0
            .0
            .compare_exchange(BUSY, IDLE, Ordering::AcqRel, Ordering::Acquire);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn claim_is_exclusive_and_released_on_drop() {
        let state = Arc::new(State::default());
        let claim = state.claim();
        assert!(claim.is_ok());
        assert!(state.is_busy());
        assert_eq!(state.claim().err(), Some(Error::Busy));
        drop(claim);
        assert!(!state.is_busy());
        assert!(state.claim().is_ok());
    }

    #[test]
    fn closed_stays_closed() {
        let state = Arc::new(State::default());
        let claim = state.claim();
        state.close();
        drop(claim);
        assert!(state.is_closed());
        assert_eq!(state.claim().err(), Some(Error::Closed));
    }
}
