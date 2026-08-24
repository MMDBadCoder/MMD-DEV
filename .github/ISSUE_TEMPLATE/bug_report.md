---
name: Bug report
about: Something behaves differently than it should
labels: bug
---

**What happened**

<!-- If it is customer-visible, quote the exact text. The last two bugs fixed
     here were both reproducible from the quoted string alone. -->

**What you expected**

**How to reproduce**

1.
2.

**Where**

- [ ] Dashboard (which page?)
- [ ] Inside a workspace
- [ ] Host setup / bring-up scripts
- [ ] Billing

**Anything from the logs**

```
journalctl -u mmd-api -u mmd-worker -n 50
```
