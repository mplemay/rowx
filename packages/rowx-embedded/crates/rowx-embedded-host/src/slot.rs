// the single session slot of an embedded database: Idle -> Active -> TearingDown -> Idle

use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SlotState {
    Idle,
    Active,
    TearingDown,
}

// what a startup that arrives in a given state does
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Admission {
    Granted,
    // the previous session is rolling back and discarding its state: wait for it
    WaitForTeardown,
    // a session's client has gone but its statement is still running in the engine: wait for it, for longer
    WaitForAbandoned,
    // a session is live: answer 53300 at once
    Busy,
}

#[must_use]
pub const fn admit(state: SlotState, active_client_gone: bool) -> Admission {
    match state {
        SlotState::Idle => Admission::Granted,
        SlotState::TearingDown => Admission::WaitForTeardown,
        SlotState::Active if active_client_gone => Admission::WaitForAbandoned,
        SlotState::Active => Admission::Busy,
    }
}

// Active -> TearingDown; any other state is a bug in the caller
#[must_use]
pub const fn begin_teardown(state: SlotState) -> Option<SlotState> {
    match state {
        SlotState::Active => Some(SlotState::TearingDown),
        SlotState::Idle | SlotState::TearingDown => None,
    }
}

// TearingDown -> Idle
#[must_use]
pub const fn finish_teardown(state: SlotState) -> Option<SlotState> {
    match state {
        SlotState::TearingDown => Some(SlotState::Idle),
        SlotState::Idle | SlotState::Active => None,
    }
}

#[derive(Debug)]
pub struct Slot {
    state: Mutex<SlotState>,
    changed: Condvar,
}

impl Default for Slot {
    fn default() -> Self {
        Self {
            state: Mutex::new(SlotState::Idle),
            changed: Condvar::new(),
        }
    }
}

impl Slot {
    /// Takes the slot for a new session. A slot whose client is alive refuses at once; a slot that is tearing down
    /// is waited for up to `teardown_wait`, and one whose client has gone but whose statement still runs up to
    /// `abandoned_wait`. Returns whether the caller now owns the (Active) slot.
    pub fn acquire(
        &self,
        teardown_wait: Duration,
        abandoned_wait: Duration,
        active_client_gone: &dyn Fn() -> bool,
    ) -> bool {
        let Ok(mut state) = self.state.lock() else {
            return false;
        };
        // each kind of wait has its own budget, counted from when that wait began: a statement that finished after a
        // long wait must still leave the full teardown time that follows it
        let mut started = Instant::now();
        let mut waiting_for = None;
        loop {
            let admission = admit(*state, *state == SlotState::Active && active_client_gone());
            if waiting_for != Some(admission) {
                waiting_for = Some(admission);
                started = Instant::now();
            }
            let limit = match admission {
                Admission::Granted => {
                    *state = SlotState::Active;
                    return true;
                }
                Admission::Busy => return false,
                Admission::WaitForTeardown => teardown_wait,
                Admission::WaitForAbandoned => abandoned_wait,
            };
            let Some(remaining) = limit
                .checked_sub(started.elapsed())
                .filter(|left| !left.is_zero())
            else {
                return false;
            };
            match self.changed.wait_timeout(state, remaining) {
                Ok((guard, _)) => state = guard,
                Err(_) => return false,
            }
        }
    }

    pub fn begin_teardown(&self) {
        self.transition(begin_teardown);
    }

    pub fn finish_teardown(&self) {
        self.transition(finish_teardown);
    }

    #[must_use]
    pub fn state(&self) -> Option<SlotState> {
        self.state.lock().ok().map(|state| *state)
    }

    fn transition(&self, step: fn(SlotState) -> Option<SlotState>) {
        if let Ok(mut state) = self.state.lock()
            && let Some(next) = step(*state)
        {
            *state = next;
            self.changed.notify_all();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use std::thread;

    const NEVER: &dyn Fn() -> bool = &|| false;

    #[test]
    fn admission_follows_the_state() {
        assert_eq!(admit(SlotState::Idle, false), Admission::Granted);
        assert_eq!(admit(SlotState::Active, false), Admission::Busy);
        assert_eq!(
            admit(SlotState::TearingDown, false),
            Admission::WaitForTeardown
        );
        // a gone client turns a refusal into a wait; it changes nothing else
        assert_eq!(admit(SlotState::Active, true), Admission::WaitForAbandoned);
        assert_eq!(admit(SlotState::Idle, true), Admission::Granted);
    }

    #[test]
    fn a_session_walks_idle_active_teardown_idle() {
        assert_eq!(
            begin_teardown(SlotState::Active),
            Some(SlotState::TearingDown)
        );
        assert_eq!(
            finish_teardown(SlotState::TearingDown),
            Some(SlotState::Idle)
        );
        // illegal edges are refused, not applied
        assert_eq!(begin_teardown(SlotState::Idle), None);
        assert_eq!(begin_teardown(SlotState::TearingDown), None);
        assert_eq!(finish_teardown(SlotState::Idle), None);
        assert_eq!(finish_teardown(SlotState::Active), None);
    }

    #[test]
    fn an_idle_slot_is_taken_and_a_live_second_client_is_refused_at_once() {
        let slot = Slot::default();
        assert!(slot.acquire(Duration::from_secs(1), Duration::from_secs(1), NEVER));
        assert_eq!(slot.state(), Some(SlotState::Active));
        let started = Instant::now();
        assert!(!slot.acquire(Duration::from_secs(1), Duration::from_secs(1), NEVER));
        assert!(started.elapsed() < Duration::from_millis(500));
    }

    #[test]
    fn a_startup_during_teardown_waits_for_the_release() {
        let slot = Arc::new(Slot::default());
        assert!(slot.acquire(Duration::ZERO, Duration::ZERO, NEVER));
        slot.begin_teardown();
        assert_eq!(slot.state(), Some(SlotState::TearingDown));
        let releaser = {
            let slot = Arc::clone(&slot);
            thread::spawn(move || {
                thread::sleep(Duration::from_millis(50));
                slot.finish_teardown();
            })
        };
        assert!(slot.acquire(Duration::from_secs(5), Duration::ZERO, NEVER));
        assert_eq!(slot.state(), Some(SlotState::Active));
        assert!(releaser.join().is_ok());
    }

    #[test]
    fn a_teardown_that_outlasts_the_wait_refuses_the_startup() {
        let slot = Slot::default();
        assert!(slot.acquire(Duration::ZERO, Duration::ZERO, NEVER));
        slot.begin_teardown();
        let started = Instant::now();
        assert!(!slot.acquire(Duration::from_millis(60), Duration::ZERO, NEVER));
        assert!(started.elapsed() >= Duration::from_millis(50));
        slot.finish_teardown();
        assert!(slot.acquire(Duration::ZERO, Duration::ZERO, NEVER));
    }

    #[test]
    fn a_slot_whose_client_is_gone_is_waited_for_and_then_taken() {
        let slot = Arc::new(Slot::default());
        assert!(slot.acquire(Duration::ZERO, Duration::ZERO, NEVER));
        let releaser = {
            let slot = Arc::clone(&slot);
            thread::spawn(move || {
                thread::sleep(Duration::from_millis(50));
                slot.begin_teardown();
                slot.finish_teardown();
            })
        };
        assert!(slot.acquire(Duration::ZERO, Duration::from_secs(5), &|| true));
        assert!(releaser.join().is_ok());
    }

    #[test]
    fn the_teardown_that_follows_an_abandoned_statement_gets_its_own_full_wait() {
        let slot = Arc::new(Slot::default());
        assert!(slot.acquire(Duration::ZERO, Duration::ZERO, NEVER));
        let releaser = {
            let slot = Arc::clone(&slot);
            thread::spawn(move || {
                thread::sleep(Duration::from_millis(300));
                slot.begin_teardown();
                thread::sleep(Duration::from_millis(100));
                slot.finish_teardown();
            })
        };
        // the abandoned wait lasts longer than the whole teardown budget, which must still start afresh
        assert!(slot.acquire(Duration::from_millis(250), Duration::from_secs(5), &|| true));
        assert!(releaser.join().is_ok());
    }

    #[test]
    fn a_gone_client_that_never_finishes_is_refused_after_the_wait() {
        let slot = Slot::default();
        assert!(slot.acquire(Duration::ZERO, Duration::ZERO, NEVER));
        let started = Instant::now();
        assert!(!slot.acquire(Duration::ZERO, Duration::from_millis(60), &|| true));
        assert!(started.elapsed() >= Duration::from_millis(50));
    }
}
