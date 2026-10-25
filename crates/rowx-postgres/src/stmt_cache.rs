use std::collections::HashMap;

/// An LRU keyed by SQL text. Capacity 0 stores nothing.
#[derive(Debug)]
pub struct StmtCache<V> {
    capacity: usize,
    tick: u64,
    entries: HashMap<String, (V, u64)>,
}

impl<V: Clone> StmtCache<V> {
    #[must_use]
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity,
            tick: 0,
            entries: HashMap::new(),
        }
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn get(&mut self, sql: &str) -> Option<V> {
        self.tick += 1;
        let tick = self.tick;
        let (value, used) = self.entries.get_mut(sql)?;
        *used = tick;
        Some(value.clone())
    }

    pub fn insert(&mut self, sql: &str, value: V) {
        if self.capacity == 0 {
            return;
        }
        self.tick += 1;
        if !self.entries.contains_key(sql)
            && self.entries.len() >= self.capacity
            && let Some(oldest) = self
                .entries
                .iter()
                .min_by_key(|(_, (_, used))| *used)
                .map(|(key, _)| key.clone())
        {
            self.entries.remove(&oldest);
        }
        self.entries.insert(sql.to_owned(), (value, self.tick));
    }

    pub fn remove(&mut self, sql: &str) {
        self.entries.remove(sql);
    }

    pub fn clear(&mut self) {
        self.entries.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn evicts_least_recently_used() {
        let mut cache = StmtCache::new(2);
        cache.insert("a", 1);
        cache.insert("b", 2);
        assert_eq!(cache.get("a"), Some(1));
        cache.insert("c", 3);
        assert_eq!(cache.get("b"), None);
        assert_eq!(cache.get("a"), Some(1));
        assert_eq!(cache.get("c"), Some(3));
        assert_eq!(cache.len(), 2);
    }

    #[test]
    fn reinserting_a_key_does_not_evict() {
        let mut cache = StmtCache::new(2);
        cache.insert("a", 1);
        cache.insert("b", 2);
        cache.insert("a", 3);
        assert_eq!(cache.get("a"), Some(3));
        assert_eq!(cache.get("b"), Some(2));
    }

    #[test]
    fn size_zero_stores_nothing() {
        let mut cache = StmtCache::new(0);
        cache.insert("a", 1);
        assert!(cache.is_empty());
        assert_eq!(cache.get("a"), None);
    }

    #[test]
    fn remove_and_clear() {
        let mut cache = StmtCache::new(4);
        cache.insert("a", 1);
        cache.insert("b", 2);
        cache.remove("a");
        assert_eq!(cache.get("a"), None);
        cache.clear();
        assert!(cache.is_empty());
    }
}
