use std::time::Duration;

use thiserror::Error;

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum OptionsError {
    #[error("max_size must be at least 1")]
    MaxSize,
    #[error("min_size must not exceed max_size")]
    MinSize,
    #[error("max_queries must be at least 1")]
    MaxQueries,
    #[error("maintain_interval must be greater than zero")]
    MaintainInterval,
}

/// The pool settings; `None` turns a limit off.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PoolOptions {
    pub min_size: usize,
    pub max_size: usize,
    pub acquire_timeout: Duration,
    pub idle_timeout: Option<Duration>,
    pub max_lifetime: Option<Duration>,
    pub max_queries: Option<u64>,
    pub ping_after: Option<Duration>,
    /// Bounds each connect attempt, which the acquire deadline bounds as a whole.
    pub connect_timeout: Option<Duration>,
    /// How often the background task reaps retired connections and refills to `min_size`.
    pub maintain_interval: Duration,
}

impl Default for PoolOptions {
    fn default() -> Self {
        Self {
            min_size: 0,
            max_size: 10,
            acquire_timeout: Duration::from_secs(30),
            idle_timeout: Some(Duration::from_secs(600)),
            max_lifetime: Some(Duration::from_secs(1800)),
            max_queries: None,
            ping_after: Some(Duration::from_secs(30)),
            connect_timeout: None,
            maintain_interval: Duration::from_secs(5),
        }
    }
}

impl PoolOptions {
    /// # Errors
    /// The first setting that is out of range.
    pub fn validate(&self) -> Result<(), OptionsError> {
        if self.max_size < 1 {
            return Err(OptionsError::MaxSize);
        }
        if self.min_size > self.max_size {
            return Err(OptionsError::MinSize);
        }
        if self.max_queries == Some(0) {
            return Err(OptionsError::MaxQueries);
        }
        if self.maintain_interval.is_zero() {
            return Err(OptionsError::MaintainInterval);
        }
        Ok(())
    }
}
