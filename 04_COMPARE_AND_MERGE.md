# 04_COMPARE_AND_MERGE.md — Compare Legacy vs. New & Apply Changes

## 🎯 YOUR TASK

The new system is built in `new_reform/`. The legacy system is in the root.

Your job is to:
1. **Compare** the legacy system and the new system.
2. **Identify** what the new system does better.
3. **Identify** what the new system is missing.
4. **Decide** if the new system is ready to replace the legacy system.

---

## 📋 COMPARISON CHECKLIST

### 1. Feature Completeness
- Does the new system have every feature of the legacy system?
- Are there any missing commands?
- Are there any missing business rules?

### 2. Code Quality
- Is the new code cleaner?
- Is it more modular?
- Is it better documented?
- Is it more testable?

### 3. Performance
- Is the new system faster?
- Does it use fewer resources?
- Does it handle failures better?

### 4. Maintainability
- Can a new engineer understand the new system in 5 minutes?
- Is it easier to add features?
- Is it easier to fix bugs?

### 5. Risk
- What are the risks of switching?
- What are the rollback options?

---

## 📝 OUTPUT FORMAT

### 1. Comparison Summary
- **Better**: List what the new system does better.
- **Worse**: List what the new system does worse.
- **Missing**: List what the new system is missing.

### 2. Migration Plan
- How to switch from legacy to new?
- What are the steps?
- What is the rollback plan?

### 3. Final Recommendation
- **Replace** → The new system is ready. Delete the legacy code and replace it.
- **Replace with caution** → The new system is better but missing X. Fix X, then replace.
- **Do not replace** → The new system is not ready. Keep the legacy system.

---

## 🚀 START

**Compare the two systems. Decide if the new system is ready to replace the legacy system. Justify your decision.**