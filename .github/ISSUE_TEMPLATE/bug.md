---
name: Bug
about: Report a defect
title: "bug: "
---

## Bug

### What happened

### What you expected

### How to reproduce

### Definition of done

- [ ] `just check` and the relevant tests pass
- [ ] New tests are named in the pull request
- [ ] Changes stay scoped to the feature
- [ ] Public Python stays typed, and the `_core` stubs are updated when a binding changes
- [ ] Errors use `thiserror` in `rowx-core`, `rowx-postgres` and `rowx-sqlite`, and are mapped to Python exceptions only
      in `errors.rs`
